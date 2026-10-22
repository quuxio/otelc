"""Official agent context propagation with unchanged ordinary application inputs."""

from __future__ import annotations

import json
import select
import subprocess
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from otelc import control, java, prepared_java
from otelc.config import Policy
from test_java_integration import Capture, wait_for_count
from test_prepared_java import binding

TASK_SOURCE = (java.ROOT / "examples/java/TaskApp.java").read_text()


def command(process: subprocess.Popen[str], value: str | None) -> str:
    """Exchange one ordinary application message, independent of producer control."""
    if value is not None:
        process.stdin.write(value + "\n")
        process.stdin.flush()
    if not select.select([process.stdout], [], [], 15)[0]:
        raise AssertionError("ordinary task application did not reply")
    return process.stdout.readline().strip()


def metric_points(capture: Capture, name: str, kind: str) -> Iterator[tuple[tuple[Any, ...], Any]]:
    """Keep resources, scopes and typed labels distinct across application processes."""
    def attributes(values: Any) -> tuple:
        return tuple(sorted((value.key, value.value.SerializeToString(deterministic=True)) for value in values))

    for request in capture.metrics:
        for resource in request.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == name and metric.HasField(kind):
                        for point in getattr(metric, kind).data_points:
                            yield (attributes(resource.resource.attributes), scope.scope.name,
                                   scope.scope.version, attributes(point.attributes)), point


def calls(capture: Capture) -> int:
    """Sum latest cumulative points rather than adding repeated SDK snapshots."""
    latest = {}
    for key, point in metric_points(capture, "otelc.function.calls", "sum"):
        latest[key] = max(latest.get(key, 0), point.as_int)
    return sum(latest.values())


class JavaTaskContextTests(unittest.TestCase):
    def test_executor_parentage_worker_reuse_cancel_reject_errors_and_live_metrics(self) -> None:
        for tasks in (False, True):
            with self.subTest(tasks=tasks):
                self.exercise(tasks)

    def exercise(self, tasks: bool) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix="otelc-ctx-", dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "TaskApp.java"
            source.write_text(TASK_SOURCE)
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "TaskApp", "classpath": ["classes"],
                "bindings": {"TaskApp": binding(source, classes / "TaskApp.class", "src/TaskApp.java")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]},
                "functions": {"include": ["TaskApp." + method for method in
                    ("parent(*)", "child(*)", "cancel()", "reject()", "cancelledChild(*)")]},
                "metrics": {"collection": False, "emission": False},
                "traces": {"enabled": True, "sample_ratio": 1.0}, "context": {"tasks": tasks},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            outcomes = []
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain, diagnostic_profile=True)
                with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertEqual(command(process, None), "ready")
                        if not plain:
                            control.wait_ready(runtime / "control.sock", process.pid)
                        values = []
                        for collection, emission in ((False, False), (True, False), (False, True), (True, True)):
                            if not plain:
                                control.request(runtime / "control.sock", "collection " + ("on" if collection else "off"))
                                status = control.request(runtime / "control.sock", "emission " + ("on" if emission else "off"))
                                self.assertEqual((status["pid"], status["collection"], status["emission"]),
                                                 (process.pid, collection, emission))
                            values.extend(command(process, value) for value in ("run", "outside", "cancel", "reject", "error"))
                        if not plain:
                            control.request(runtime / "control.sock", "flush")
                            self.assertEqual(wait_for_count(lambda: calls(capture), 14), 14)
                        stdout, stderr = process.communicate("quit\n", timeout=15)
                        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
                        launch.verify_inputs()
                        outcomes.append(values)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            self.assertEqual(outcomes[0], [value for index in range(1, 5)
                for value in ("8", "21", "true", "true", f"true:{index}")])
            self.assertEqual(outcomes[0], outcomes[1])
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            parents = {span.span_id: span for span in spans if span.name == "TaskApp.parent"}
            children = [span for span in spans if span.name == "TaskApp.child"]
            self.assertEqual(len(parents), 8)
            self.assertEqual(len(children), 12)
            self.assertFalse(any(span.name == "TaskApp.cancelledChild" for span in spans))
            linked = [span for span in children if span.parent_span_id in parents]
            self.assertEqual(len(linked), 8 if tasks else 0)
            self.assertEqual(sum(not span.parent_span_id for span in children), 4 if tasks else 12)
            for span in linked:
                self.assertEqual(span.trace_id, parents[span.parent_span_id].trace_id)
