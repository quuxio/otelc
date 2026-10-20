"""Reject stale, missing and mismatched MIR qualification evidence."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import rust_mir_coverage as gate
from test_rust_llvm_coverage import fixture as shared_fixture


def fixture(root: Path) -> None:
    shared_fixture(root)
    for name in ("native/rust/mir/Cargo.toml", "native/rust/mir/Cargo.lock",
                 "ci/build_rust_mir.py", "ci/rust_mir_coverage.py", "Makefile",
                 ".github/workflows/ci.yml"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("maintained source")


def report() -> str:
    return "".join("SF:/current/" + name + "\nDA:1,1\n"
                   for name in gate.COMPONENTS)


def artifact(name: str, executable: str) -> str:
    return json.dumps({"reason": "compiler-artifact", "target": {"name": name},
                       "executable": executable}) + "\n"


class MirCoverageTests(unittest.TestCase):
    def test_environment_uses_pinned_tools_and_refuses_bootstrap_or_wrong_versions(self):
        def output(arguments, _):
            return "cargo 1.98.1 (qualified)" if arguments[0] == "cargo" else "LLVM version 22.1.8"

        with patch.dict(os.environ, {"RUSTFLAGS": "wrong", "RUSTC_WRAPPER": "wrong",
                "OTELC_RUST_PLAN": "wrong", "CARGO_LLVM_COV": "wrong", "KEEP": "value"}, clear=True), patch.object(gate.builder, "qualified_rustc", return_value=Path("/qualified/rustc")), patch.object(gate, "llvm_environment", side_effect=lambda values: values.update(LLVM_COV="/llvm-cov", LLVM_PROFDATA="/llvm-profdata")), patch.object(gate.builder, "output", side_effect=output):
            values = gate.environment()
            self.assertEqual(values["KEEP"], "value")
            self.assertEqual(values["RUSTC"], "/qualified/rustc")
            self.assertFalse(any(key.startswith(("OTELC_", "CARGO_LLVM_COV")) for key in values))
            self.assertNotIn("RUSTC_WRAPPER", values)
            self.assertNotIn("RUSTFLAGS", values)
            with patch.object(gate.builder, "output", return_value="cargo other"):
                with self.assertRaisesRegex(ValueError, "Cargo 1.98.1"):
                    gate.environment()
            with patch.object(gate.builder, "output", side_effect=["cargo 1.98.1 pinned", "LLVM version 23.1.1"]):
                with self.assertRaisesRegex(ValueError, "LLVM 22.1.8"):
                    gate.environment()
        with patch.dict(os.environ, {"RUSTC_BOOTSTRAP": "1"}):
            with self.assertRaisesRegex(ValueError, "BOOTSTRAP to be unset"):
                gate.environment()

    def test_command_preserves_failure_channels_and_timeout_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "command.log"
            for code in (0, 7):
                result = subprocess.CompletedProcess(["tool"], code, b"stdout")
                with patch.object(gate.subprocess, "run", return_value=result):
                    if code:
                        with self.assertRaises(subprocess.CalledProcessError):
                            gate.command(["tool"], root, {}, log)
                    else:
                        self.assertEqual(gate.command(["tool"], root, {}, log), "stdout")
                self.assertEqual(log.read_bytes(), b"stdout")
                self.assertTrue(log.with_suffix(".stderr").exists())
            with patch.object(gate.subprocess, "run", side_effect=subprocess.TimeoutExpired("tool", 900, output=b"partial")):
                with self.assertRaises(subprocess.TimeoutExpired):
                    gate.command(["tool"], root, {}, log)
            self.assertEqual(log.read_bytes(), b"partial")

    def test_cargo_requires_one_actual_named_test_executable(self):
        self.assertEqual(gate.executable(artifact("compiler", "/tests"), "compiler"), Path("/tests"))
        for output in ("", artifact("other", "/wrong"), artifact("compiler", "/one") + artifact("compiler", "/two")):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                gate.executable(output, "compiler")

    def test_export_uses_only_the_matching_map_and_refuses_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "current"; binary.write_bytes(b"map")
            manifest = gate.retain_group(root / "group", binary)
            values = {"LLVM_COV": "/cov", "LLVM_PROFDATA": "/profdata"}
            with self.assertRaisesRegex(ValueError, "no valid fresh"):
                gate.export_group(manifest, values)
            raw = manifest.parent / "profiles/execution.profraw"; raw.write_bytes(b"counts")
            def process(arguments, **_):
                return subprocess.CompletedProcess(arguments, 0, report().encode(), b"")
            with patch.object(gate.subprocess, "run", side_effect=process) as run:
                self.assertEqual(gate.export_group(manifest, values), report())
                self.assertIn("--fatal-warnings", run.call_args.args[0])
                self.assertEqual(run.call_args_list[0].args[0].count(str(raw.resolve())), 1)
                diagnostic = subprocess.CompletedProcess([], 0, report().encode(), b"warning")
                with patch.object(gate.subprocess, "run", return_value=diagnostic):
                    with self.assertRaisesRegex(ValueError, "diagnostics"):
                        gate.export_group(manifest, values)
            (manifest.parent / "application").write_bytes(b"stale")
            with self.assertRaisesRegex(ValueError, "identity"):
                gate.export_group(manifest, values)
            (manifest.parent / "application").write_bytes(b"map")
            raw.unlink(); raw.symlink_to(binary)
            with self.assertRaisesRegex(ValueError, "no valid fresh"):
                gate.export_group(manifest, values)
            raw.unlink()
            record = json.loads(manifest.read_text()); record["object"] = "../current"
            manifest.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "location"):
                gate.export_group(manifest, values)

    def test_every_component_needs_nonempty_80_percent_coverage(self):
        gate.enforce_components(report())
        for source in (report().replace("DA:1,1", "DA:1,0"), "SF:/other.rs\nDA:1,1\n"):
            with self.assertRaises(ValueError):
                gate.enforce_components(source)

    def test_sdk_build_uses_real_coverage_fingerprint_and_retained_test_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); binary = root / "sdk-tests"; binary.write_bytes(b"tests")
            run = root / "run"; run.mkdir()
            with patch.object(gate, "command", side_effect=[artifact("quux_otelc_rust", str(binary)), "tests passed", "built"]) as command:
                sdk, manifest = gate.compile_sdk(root, root / "target", run, {"RUSTC": "/pinned/rustc"}, None)
            self.assertTrue(manifest.exists())
            self.assertEqual(sdk, root / "target/sdk/debug/libquux_otelc_rust.rlib")
            self.assertTrue(all(call.args[2]["RUSTFLAGS"] == "-C instrument-coverage" for call in command.call_args_list))
            self.assertIn(str(run), command.call_args_list[1].args[2]["LLVM_PROFILE_FILE"])
            with self.assertRaisesRegex(ValueError, "SDK coverage requires"):
                gate.compile_sdk(root, root / "target", run, {"RUSTC": "/pinned/rustc"}, {"RUSTC_BOOTSTRAP": "1"})

    def test_helper_bootstrap_is_build_only_and_instruments_the_actual_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "target"; run = root / "run"; run.mkdir()
            driver = target / "helper/debug/otelc-rust-mir"; driver.parent.mkdir(parents=True); driver.write_bytes(b"driver")
            values = {"RUSTC": "/qualified/bin/rustc"}
            with patch.object(gate, "command", side_effect=[artifact("compiler", "/current/tests"), "built"]) as command:
                tests, manifest = gate.compile_helper(root, target, run, values)
            self.assertEqual(tests, Path("/current/tests"))
            self.assertEqual((manifest.parent / "application").read_bytes(), b"driver")
            self.assertIn("instrument-coverage", command.call_args.args[0])
            self.assertTrue(all(call.args[2]["RUSTC_BOOTSTRAP"] == "otelc_rust_mir" for call in command.call_args_list))
            self.assertNotIn("RUSTC_BOOTSTRAP", values)

    def test_pairs_require_all_executed_cases_and_bind_compiler_sdk_and_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = root / "run"; run.mkdir()
            compiler = root / "rustc"; compiler.write_bytes(b"compiler")
            sdk = root / "sdk"; sdk.write_bytes(b"sdk")
            driver = root / "driver"; driver.write_bytes(b"driver")
            helper = gate.retain_group(run / "helper", driver)
            def execute(_, __, values, ___):
                applications = Path(values["OTELC_RUST_MIR_COVERAGE"])
                for number in range(gate.EXPECTED_PAIRS):
                    p = applications / str(number) / "instrumented"; p.mkdir(parents=True)
                    (p / "coverage.json").write_text("{}")
                self.assertNotIn("RUSTC_BOOTSTRAP", values)
                return "tests passed"
            with patch.object(gate, "command", side_effect=execute):
                groups = gate.run_pairs(root, run, {"RUSTC": str(compiler)}, sdk, root / "tests", helper)
            self.assertEqual(len(groups), gate.EXPECTED_PAIRS)
            profile = json.loads((run / "profile.json").read_text())
            self.assertEqual(profile["sdk_sha256"], gate.builder.fingerprint(sdk))
            self.assertEqual(profile["driver_sha256"], gate.builder.fingerprint(helper.parent / "application"))
            other = root / "other"; other.mkdir()
            with patch.object(gate, "command", return_value="zero tests"):
                with self.assertRaisesRegex(ValueError, "executed compiler pairs"):
                    gate.run_pairs(root, other, {"RUSTC": str(compiler)}, sdk, root / "tests", helper)

    def test_qualification_publishes_only_after_all_execution_and_source_guards_pass(self):
        for failure in (None, "source", "build", "export", "tool", "sdk"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); fixture(root)
                values = {}
                for key in ("RUSTC", "LLVM_COV", "LLVM_PROFDATA"):
                    path = root / key
                    path.write_text("qualified tool")
                    values[key] = str(path)
                (root / "sdk").write_text("qualified SDK")
                output = root / "build/rust-mir-coverage.lcov"; output.parent.mkdir(); output.write_text("old")
                output.with_suffix(".json").write_text("old summary")
                before = gate.source_hashes(root)
                def source_hashes(_):
                    return dict(before, drift="changed") if failure == "source" and source_hashes.called else before
                source_hashes.called = False
                def exported(*_):
                    source_hashes.called = True
                    if failure == "export": raise ValueError("missing execution")
                    if failure == "tool": (root / "RUSTC").write_text("changed compiler")
                    if failure == "sdk": (root / "sdk").write_text("changed SDK")
                    return report()
                build_error = subprocess.CalledProcessError(1, "cargo") if failure == "build" else None
                with patch.object(gate, "environment", return_value=values), patch.object(gate, "compile_sdk", return_value=(root / "sdk", root / "unit"), side_effect=build_error), patch.object(gate, "compile_helper", return_value=(root / "tests", root / "helper")), patch.object(gate, "run_pairs", return_value=[root / "app"] * gate.EXPECTED_PAIRS), patch.object(gate, "export_group", side_effect=exported), patch.object(gate, "source_hashes", side_effect=source_hashes):
                    if failure:
                        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                            gate.run(root)
                        self.assertFalse(output.exists())
                        self.assertFalse(output.with_suffix(".json").exists())
                    else:
                        self.assertEqual(gate.run(root), gate.merge_line_reports([report()]))
                        record = json.loads(output.with_suffix(".json").read_text())
                        self.assertEqual(record["sources"], before)
                        self.assertEqual(record["compiler_pairs"], gate.EXPECTED_PAIRS)
                        self.assertEqual(record["report_sha256"], gate.builder.fingerprint(output))
                self.assertEqual(len(list((root / "target/rust-mir-coverage").glob("run-*/previous-*"))), 2)

    def test_concurrent_run_cannot_touch_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root / "target/rust-mir-coverage"; target.mkdir(parents=True)
            with (target / "qualification.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(ValueError, "already running"):
                    gate.run(root)
            self.assertFalse(list(target.glob("run-*")))
