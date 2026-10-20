"""Check stable compiler identity and the private bootstrap boundary."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import build_rust_mir as builder


class RustMirBuildTests(unittest.TestCase):
    def test_resolves_the_full_stable_identity_and_rejects_proxy_changes(self):
        version = (f"rustc 1.98.1 (48a229cea 2026-09-17)\ncommit-hash: {builder.RUST_COMMIT}\n"
                   "host: aarch64-apple-darwin\nLLVM version: 22.1.8")
        with tempfile.TemporaryDirectory() as directory:
            compiler = Path(directory) / "bin/rustc"
            compiler.parent.mkdir()
            compiler.write_bytes(b"compiler")
            with patch.object(builder.shutil, "which", return_value="/proxy"), patch.object(
                    builder, "output", side_effect=[version, directory, version]):
                self.assertEqual(builder.qualified_rustc("rustc", {}), compiler.resolve())
            for invalid in [version.replace(builder.RUST_COMMIT, "other"),
                            version.replace("22.1.8", "22.1.9"),
                            version.replace("aarch64", "x86_64"),
                            version.replace("1.98.1", "1.98.2")]:
                with self.subTest(version=invalid), patch.object(builder.shutil, "which", return_value="/proxy"), patch.object(builder, "output", return_value=invalid):
                    with self.assertRaises(ValueError):
                        builder.qualified_rustc("rustc", {})
            with patch.object(builder.shutil, "which", return_value=None):
                with self.assertRaises(ValueError):
                    builder.qualified_rustc("missing", {})
            with patch.object(builder.shutil, "which", return_value="/proxy"), patch.object(builder, "output", side_effect=[version, directory, "different"]):
                with self.assertRaises(ValueError):
                    builder.qualified_rustc("rustc", {})

    def test_build_bootstraps_only_the_private_helper_and_fingerprints_every_input(self):
        with tempfile.TemporaryDirectory(prefix="Rust MIR spaces ") as directory:
            root = Path(directory)
            destination = root / "target/debug"
            destination.mkdir(parents=True)
            sdk = destination / "libquux_otelc_rust.rlib"
            sdk.write_bytes(b"stable SDK")
            compiler = root / "compiler/bin/rustc"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"qualified compiler")
            environment = {"OTELC_RUSTC": "/optional/nightly/rustc"}

            def compile_helper(arguments, **kwargs):
                self.assertEqual(arguments[:2], ["cargo", "rustc"])
                self.assertIn("--locked", arguments)
                self.assertEqual(kwargs["env"]["RUSTC"], str(compiler))
                self.assertEqual(kwargs["env"]["RUSTC_BOOTSTRAP"], "otelc_rust_mir")
                self.assertTrue(kwargs["check"])
                driver = root / "target/rust-mir-frontend/debug/otelc-rust-mir"
                driver.parent.mkdir(parents=True)
                driver.write_bytes(b"compiler driver")

            with patch.object(builder, "qualified_rustc", return_value=compiler), patch.object(builder.subprocess, "run", side_effect=compile_helper):
                profile = builder.build(root, destination, environment)
            self.assertNotIn("RUSTC_BOOTSTRAP", environment)
            self.assertEqual(json.loads((destination / builder.PROFILE).read_text()), profile)
            for key, payload in [("sdk", b"stable SDK"), ("rustc", b"qualified compiler"),
                                 ("driver", b"compiler driver")]:
                self.assertEqual(profile[key + "_sha256"], hashlib.sha256(payload).hexdigest())
            self.assertFalse((destination / (builder.PROFILE + ".pending")).exists())

    def test_unqualified_environment_missing_sdk_and_failed_build_publish_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "output"
            for key in ["RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS",
                        "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET", "RUSTC_BOOTSTRAP"]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    builder.build(root, destination, {key: "unexpected"})
            with patch.object(builder, "qualified_rustc", return_value=Path("/compiler/bin/rustc")):
                with self.assertRaises(ValueError):
                    builder.build(root, destination, {})
                (destination / "libquux_otelc_rust.rlib").write_bytes(b"SDK")
                failure = subprocess.CalledProcessError(1, "cargo")
                with patch.object(builder.subprocess, "run", side_effect=failure):
                    with self.assertRaises(subprocess.CalledProcessError):
                        builder.build(root, destination, {})
            self.assertFalse((destination / builder.PROFILE).exists())
