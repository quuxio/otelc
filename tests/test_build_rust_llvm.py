"""Validate compiler identity, isolated SDK builds and descriptor publication."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import build_rust_llvm as builder


class RustCompilerBuildTests(unittest.TestCase):
    def test_qualified_compiler_resolves_the_real_toolchain_not_a_moving_proxy(self):
        version = f"rustc 1.101.0-nightly (d080e7dff 2026-09-27)\ncommit-hash: {builder.RUST_COMMIT}\nhost: aarch64-apple-darwin\nLLVM version: 23.1.1"
        with tempfile.TemporaryDirectory() as directory:
            compiler = Path(directory) / "bin/rustc"
            compiler.parent.mkdir(); compiler.write_bytes(b"compiler")
            with patch.object(builder.shutil, "which", return_value="/rustup/proxy"), patch.object(builder, "output", side_effect=[version, directory, version]):
                self.assertEqual(builder.qualified_rustc("rustc"), compiler.resolve())
            variants = [version.replace(builder.RUST_COMMIT, "different"),
                        version.replace("23.1.1", "23.1.2"), version.replace("aarch64", "x86_64"),
                        version.replace("1.101.0-nightly", "1.101.0")]
            for invalid in variants:
                with self.subTest(version=invalid), patch.object(builder.shutil, "which", return_value="/rustup/proxy"), patch.object(builder, "output", return_value=invalid):
                    with self.assertRaises(ValueError): builder.qualified_rustc("rustc")
            with patch.object(builder.shutil, "which", return_value=None):
                with self.assertRaises(ValueError): builder.qualified_rustc("missing")
            with patch.object(builder.shutil, "which", return_value="/rustup/proxy"), patch.object(builder, "output", side_effect=[version, directory, "changed"]):
                with self.assertRaises(ValueError): builder.qualified_rustc("rustc")

    def test_profile_records_actual_inputs_without_overwriting_the_stable_sdk(self):
        with tempfile.TemporaryDirectory(prefix="rust profile spaces ") as directory:
            root = Path(directory); destination = root / "target/debug"
            sdk = root / "target/rust-llvm-sdk/debug/libquux_otelc_rust.rlib"
            plugin = destination / "otelc_rust_pass.dylib"

            def query(arguments):
                return {"--version": "23.1.3", "--bindir": str(root / "LLVM tools"),
                        "--cxxflags": '-std=c++17 -I"include with spaces"',
                        "--ldflags": '-L"libraries with spaces" -lLLVM'}[arguments[1]]

            def compile_input(arguments, **kwargs):
                self.assertTrue(kwargs["check"])
                if arguments[0] == "cargo":
                    self.assertIn("--locked", arguments)
                    self.assertEqual(kwargs["env"]["RUSTC"], "/qualified/rustc")
                    sdk.parent.mkdir(parents=True); sdk.write_bytes(b"nightly SDK")
                else:
                    self.assertIn("-Iinclude with spaces", arguments)
                    self.assertIn("-Llibraries with spaces", arguments)
                    self.assertNotIn("shell", kwargs)
                    plugin.write_bytes(b"LLVM pass")

            with patch.object(builder, "qualified_rustc", return_value=Path("/qualified/rustc")), patch.object(builder, "output", side_effect=query), patch.object(builder.subprocess, "run", side_effect=compile_input):
                profile = builder.build(root, destination, {})
            self.assertEqual(profile["sdk_sha256"], hashlib.sha256(b"nightly SDK").hexdigest())
            self.assertEqual(profile["plugin_sha256"], hashlib.sha256(b"LLVM pass").hexdigest())
            self.assertEqual(json.loads((destination / "otelc-rust-llvm.json").read_text()), profile)
            self.assertEqual(len(profile["protected_source_roots"]), 3)
            self.assertFalse((destination / "libquux_otelc_rust.rlib").exists())

    def test_unqualified_environments_and_build_failure_do_not_publish_a_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); destination = root / "output"
            for key in ("RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_TARGET"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    builder.build(root, destination, {key: "unexpected"})
            with patch.object(builder, "qualified_rustc", return_value=Path("/qualified/rustc")), patch.object(builder, "output", return_value="wrong LLVM"):
                with self.assertRaises(ValueError): builder.build(root, destination, {})
            with patch.object(builder, "qualified_rustc", return_value=Path("/qualified/rustc")), patch.object(builder, "output", side_effect=["23.1.3", "/llvm/bin"]), patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "cargo")):
                with self.assertRaises(subprocess.CalledProcessError): builder.build(root, destination, {})
            self.assertFalse((destination / "otelc-rust-llvm.json").exists())
