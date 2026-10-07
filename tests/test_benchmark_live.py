"""Verify paired sample accounting, protocol failures and benchmark evidence gates."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import benchmark_live as live


class Process:
    def __init__(self, pid=1, line="ready\n", exit_code=0, running=False):
        self.pid = pid
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(line)
        self.exit_code = exit_code
        self.running = running
        self.terminated = False

    def wait(self, timeout):
        self.running = False
        return self.exit_code

    def poll(self):
        return None if self.running else self.exit_code

    def terminate(self):
        self.terminated = True
        self.running = False


class LiveBenchmarkTests(unittest.TestCase):
    def test_bounded_response_and_sample_parsing(self):
        for response, expected in [("elapsed_ns=10 checksum=7 calls=4\n", 10), ("elapsed_ns=0 checksum=7 calls=4\n", None), ("elapsed_ns=10 checksum=7 calls=3\n", None), ("", None)]:
            process = Process(line=response)
            with patch.object(live.select, "select", return_value=([process.stdout], [], [])):
                if expected is None:
                    with self.assertRaises(ValueError):
                        live.batch(process, 4)
                else:
                    self.assertEqual(live.batch(process, 4)["elapsed_ns"], expected)
                    self.assertEqual(process.stdin.getvalue(), "batch 4\n")
        with patch.object(live.select, "select", return_value=([], [], [])):
            with self.assertRaises(ValueError):
                live.read_line(Process())

    def test_control_checks_process_identity(self):
        with patch.object(live.subprocess, "check_output", return_value='{"pid":4,"metrics_enabled":false}'):
            self.assertFalse(live.control(Path("cli"), Path("socket"), "status", 4)["metrics_enabled"])
            with self.assertRaises(ValueError):
                live.control(Path("cli"), Path("socket"), "status", 5)

    def test_alternating_phases_checksum_and_toggle_failures(self):
        plain, instrumented = Process(1), Process(2)
        order = []
        state = {"enabled": False}
        def control(_cli, _socket, command, pid):
            self.assertEqual(pid, 2)
            order.append(command)
            state["enabled"] = command == "enable"
            return {"metrics_enabled": state["enabled"]}
        def batch(process, iterations):
            return {"elapsed_ns": 10 if process.pid == 1 else 30 if state["enabled"] else 20, "checksum": 9, "calls": iterations}
        with patch.object(live, "control", side_effect=control), patch.object(live, "batch", side_effect=batch):
            samples = live.measure("cli", "socket", plain, instrumented, 4, 2)
        self.assertEqual(order, ["disable", "enable", "enable", "disable"])
        result = live.compare(samples, 4)
        self.assertEqual(result["metrics_added_ns_per_call"], 2.5)
        self.assertEqual(result["total_instrumentation_ns_per_call"], 5)
        self.assertEqual(result["metrics_change_percent"], 50)
        with patch.object(live, "control", return_value={"metrics_enabled": True}), patch.object(live, "batch", side_effect=batch):
            with self.assertRaises(ValueError):
                live.measure("cli", "socket", plain, instrumented, 4, 2)
        bad = [{"elapsed_ns": 10, "checksum": 9, "calls": 4}, {"elapsed_ns": 10, "checksum": 8, "calls": 4}]
        with patch.object(live, "control", side_effect=control), patch.object(live, "batch", side_effect=bad):
            with self.assertRaises(ValueError):
                live.measure("cli", "socket", plain, instrumented, 4, 2)

    def setup_run(self, root, output, evidence):
        (root / "examples/apps").mkdir(parents=True)
        (root / "target/debug").mkdir(parents=True)
        (root / "examples/apps/live-latency.cpp").write_text("unchanged input")
        (root / "examples/live.toml").write_text('socket="build/control/metrics.sock"')
        (root / "target/debug/otelc-llvm-toolchain.json").write_text('{"bindir":"/compiler"}')
        output.mkdir()
        (output / "runtime.json").write_text(json.dumps(evidence))

    def test_end_to_end_driver_and_evidence_failures(self):
        evidence = {"drained": True, "export_finished": True, "export_dropped_batches": 0, "losses": {"queue": 0}, "function_calls": 3000}
        for failure in [None, "telemetry", "source", "exit", "startup"]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root, output = Path(temporary), Path(temporary) / "output"
                current = {**evidence, "drained": failure != "telemetry"}
                self.setup_run(root, output, current)
                processes = [Process(1, exit_code=1 if failure == "exit" else 0, running=True), Process(2, running=True)]
                samples = {key: [{"elapsed_ns": n, "checksum": 9, "calls": 1000}] for key, n in [("baseline", 10), ("metrics_off", 20), ("metrics_on", 30)]}
                def measure(*_args):
                    if failure == "source":
                        (root / "examples/apps/live-latency.cpp").write_text("modified")
                    return samples
                with patch.object(live.platform, "platform", return_value="Test host"), patch.object(live.platform, "machine", return_value="arm64"), patch.object(live.subprocess, "run"), patch.object(live.subprocess, "check_output", return_value="clang test\n"), patch.object(live.subprocess, "Popen", side_effect=processes), patch.object(live, "read_line", return_value="bad" if failure == "startup" else "ready"), patch.object(live, "batch"), patch.object(live, "control"), patch.object(live, "measure", side_effect=measure):
                    if failure:
                        with self.assertRaises(ValueError):
                            live.run(root, output, 1000, 2)
                    else:
                        result = live.run(root, output, 1000, 2)
                        self.assertTrue(result["complete_telemetry"])
                        self.assertEqual(result["same_instrumented_pid"], 2)
                        self.assertEqual(json.loads((output / "report.json").read_text())["runtime"]["function_calls"], 3000)
                if failure == "startup":
                    self.assertTrue(processes[0].terminated)
        for iterations, runs in [(0, 8), (1000001, 8), (1000, 1), (1000, 101)]:
            with self.assertRaises(ValueError):
                live.run(Path("unused"), Path("unused"), iterations, runs)

    def test_main(self):
        with patch("sys.argv", ["benchmark_live", "--iterations", "1000", "--runs", "2"]), patch.object(live, "run", return_value={"summary": {"metrics_added_ns_per_call": 5}}), contextlib.redirect_stdout(io.StringIO()) as output:
            live.main()
        self.assertIn("metrics_added_ns_per_call", output.getvalue())


if __name__ == "__main__":
    unittest.main()
