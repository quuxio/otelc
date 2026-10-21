"""Missing optional types must not stop unrelated selected function probes from ending."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import health, java, prepared_java
from otelc.config import Policy
from test_java_integration import Capture
from test_prepared_java import binding

SOURCE = '''class OptionalType {}
public final class Caller {
  static int workload() { return 7; }
  static int withNull(OptionalType ignored) { return 8; }
  static Object returnsNull() { return null; }
  static OptionalType unused() { return null; }
  public static void main(String[] args) {
    int checksum = workload() + withNull(null);
    System.out.println("{\\"checksum\\":" + checksum + ",\\"null_result\\":" + (returnsNull() == null) + "}");
  }
}
'''


class MethodOriginTests(unittest.TestCase):
    def test_missing_unrelated_and_parameter_types_do_not_leak_spans(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "Caller.java"
            source.write_text(SOURCE)
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            (classes / "OptionalType.class").unlink()
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Caller", "classpath": ["classes"],
                "bindings": {"Caller": binding(source, classes / "Caller.class", "src/Caller.java")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]},
                "functions": {"include": ["Caller.workload()", "Caller.withNull(*)", "Caller.returnsNull()"]},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain, diagnostic_profile=True)
                result = subprocess.run(launch.argv, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"checksum": 15, "null_result": True})
                self.assertEqual(result.stderr, "")
                launch.verify_inputs()
                self.assertFalse((classes / "OptionalType.class").exists())
                if plain:
                    self.assertFalse(capture.metrics)
                    self.assertFalse(capture.traces)
                else:
                    report = health.read(runtime / "health.json")
                    self.assertEqual(report["span_accounting"]["sampled_active"], "0")
                    self.assertEqual(report["span_accounting"]["queue_dropped"], "0")
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(sorted(span.name for span in spans),
                             ["Caller.returnsNull", "Caller.withNull", "Caller.workload"],
                             (root / "instrumented/agent.log").read_text())
            self.assertEqual(capture.calls(), 1)
            counts = {next(item.value.string_value for item in point.attributes if item.key == "function"): point.as_int
                      for point in capture.points("otelc.function.calls")}
            self.assertEqual(counts, {"workload": 1, "withNull": 1, "returnsNull": 1})
