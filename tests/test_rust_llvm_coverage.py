"""Keep compiler coverage fresh, isolated by LLVM version and independently gated."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import rust_llvm_coverage as gate


COMPONENTS = ("crates/rust-adapter/src/compiler_inputs.rs", "crates/rust-adapter/src/compiler_profile.rs",
              "crates/rust-adapter/src/compiler_llvm.rs", "crates/rust-adapter/src/compiler_frontend.rs",
              "native/rust/frontend/src/main.rs", "native/rust/OtelcRustPass.cpp")


def fixture(root: Path) -> None:
    (root / "Cargo.toml").write_text("manifest")
    (root / "Cargo.lock").write_text("lockfile")
    (root / "crates/example").mkdir(parents=True)
    (root / "crates/example/lib.rs").write_text("fn selected() {}")
    for name in ('native/rust/frontend/Cargo.toml', 'native/rust/frontend/Cargo.lock',
                 'ci/build_rust_llvm.py', 'ci/rust_llvm_coverage.py', 'ci/rust_coverage.py'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('source fixture')


def report() -> str:
    return "".join("SF:/original/" + name + "\nDA:1,1\n" for name in COMPONENTS)


class CompilerCoverageTests(unittest.TestCase):
    def test_environment_requires_qualified_tools_and_removes_coverage_wrappers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); descriptor = root / "target/debug/otelc-rust-llvm.json"
            descriptor.parent.mkdir(parents=True); descriptor.write_text('{"rustc":"/profile/compiler"}')
            with patch.object(gate.builder, "output", return_value="cargo 1.98.1 (qualified)"), patch.object(gate.builder, "qualified_rustc", return_value=Path("/qualified/rustc")):
                with patch.dict(os.environ, {"RUSTC_WRAPPER": "wrong", "__CARGO_LLVM_COV_PRIVATE": "wrong", "CARGO_LLVM_COV": "1", "CARGO_BUILD_TARGET": "other", "KEEP": "value"}, clear=True):
                    environment, config = gate.environment(root, root / "coverage")
                self.assertEqual(environment["RUSTC"], "/qualified/rustc")
                self.assertEqual(environment["KEEP"], "value")
                self.assertFalse(any(key.startswith(("CARGO_LLVM_COV", "__CARGO_LLVM_COV")) for key in environment))
                self.assertNotIn("RUSTC_WRAPPER", environment)
                self.assertNotIn("CARGO_BUILD_TARGET", environment)
                self.assertEqual(environment["RUSTFLAGS"], "-C instrument-coverage")
                self.assertIn("llvm@23", config)
                descriptor.write_bytes(b"x" * 65537)
                with self.assertRaisesRegex(ValueError, "64 KiB"): gate.environment(root, root / "coverage")
            with patch.object(gate.builder, "output", return_value="cargo other"):
                with self.assertRaisesRegex(ValueError, "Cargo 1.98.1"): gate.environment(root, root / "coverage")

    def test_each_compiler_component_needs_nonempty_80_percent_execution_evidence(self):
        self.assertEqual(gate.COMPONENTS, COMPONENTS)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory); (target / "debug/deps").mkdir(parents=True)
            executable = target / "debug/deps/profile-test"; executable.write_text("map"); executable.chmod(0o700)
            (target / "execution.profraw").write_bytes(b"profile")
            for source, passes in ((report(), True), (report().replace("DA:1,1", "DA:1,0"), False), ("SF:/original/other.rs\nDA:1,1\n", False)):
                with patch.object(gate.subprocess, "run"), patch.object(gate.subprocess, "check_output", return_value=source) as export:
                    if passes: gate.export_report(target, Path("/LLVM23"), {}, target / "pass.dylib")
                    else:
                        with self.assertRaises(ValueError): gate.export_report(target, Path("/LLVM23"), {}, target / "pass.dylib")
                self.assertTrue(all(call.args[0][0] == "/LLVM23/llvm-cov" and call.args[0].count("--object") == 1 for call in export.call_args_list))
            (target / "execution.profraw").unlink()
            with self.assertRaisesRegex(ValueError, "no execution profiles"):
                gate.export_report(target, Path("/LLVM23"), {}, target / "pass.dylib")

    def test_success_clears_old_profiles_runs_ignored_qualification_and_publishes_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fixture(root); target = root / "target/rust-llvm-coverage"
            (target / "retained").mkdir(parents=True)
            (target / "retained/old").write_text("old map")
            (target / "old.profraw").write_text("old profile")
            with patch.object(gate, "environment", return_value=({"RUSTC": "/qualified/rustc", "RUSTFLAGS": "-C instrument-coverage"}, "/llvm-config")), patch.object(gate.builder, "llvm_tools", return_value=Path("/LLVM23")), patch.object(gate.builder, "compile_pass", return_value=target / "pass.dylib"), patch.object(gate.builder, "compile_frontend", return_value=target / "frontend") as frontend, patch.object(gate.builder, "write_profile"), patch.object(gate.subprocess, "run") as run, patch.object(gate, "export_report", return_value=report()):
                self.assertEqual(gate.run(root), report())
            self.assertEqual(frontend.call_args.args[3]['RUSTFLAGS'],
                             '-C instrument-coverage -C metadata=otelc_frontend_coverage -C link-dead-code')
            self.assertFalse((target / "old.profraw").exists()); self.assertFalse((target / "retained/old").exists())
            qualification = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ['cargo', 'test']]
            self.assertEqual(len(qualification), 1)
            self.assertIn("--include-ignored", qualification[0])
            self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))
            output = root / "build/rust-llvm-coverage.lcov"
            record = json.loads(output.with_suffix(".json").read_text())
            self.assertEqual(record["report_sha256"], gate.builder.fingerprint(output))
            self.assertEqual(record["sources"], gate.source_hashes(root))

    def test_build_failure_and_source_changes_never_publish_success(self):
        for changed in (False, True, 'frontend-lock'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); fixture(root)

                def compile_step(*_, **__):
                    if not changed: raise subprocess.CalledProcessError(1, "cargo")
                    path = 'native/rust/frontend/Cargo.lock' if changed == 'frontend-lock' else 'crates/example/lib.rs'
                    (root / path).write_text("changed during qualification")

                with patch.object(gate, "environment", return_value=({"RUSTC": "/qualified/rustc", "RUSTFLAGS": "-C instrument-coverage"}, "/llvm-config")), patch.object(gate.builder, "llvm_tools", return_value=Path("/LLVM23")), patch.object(gate.builder, "compile_pass", return_value=Path("/pass")), patch.object(gate.builder, "compile_frontend", return_value=Path("/frontend")), patch.object(gate.builder, "write_profile"), patch.object(gate.subprocess, "run", side_effect=compile_step), patch.object(gate, "export_report", return_value=report()):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)): gate.run(root)
                self.assertFalse((root / "build/rust-llvm-coverage.json").exists())

    def test_concurrent_runs_are_refused_instead_of_deleting_each_others_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "target/rust-llvm-coverage"; target.mkdir(parents=True)
            with (target / "qualification.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "already running"): gate.run(root)
