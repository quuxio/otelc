"""Runtime health validates evidence shape and survives actual agent shutdown."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from otelc import control, health, java
from otelc.config import Policy
from test_java_integration import Capture, batch


def report() -> dict[str, Any]:
    """A structurally complete report; it deliberately makes no delivery claim."""
    exports = {"exporters_observed": 1, "unit": "spans", "batches_started": "0",
               "batches_succeeded": "0", "batches_failed": "0", "batches_pending": "0",
               "units_started": "0", "units_succeeded": "0", "units_failed": "0"}
    return {"schema_version": 1, "kind": "otelc.runtime-health", "language": "java",
            "pid": os.getpid(), "policy_sha256": "a" * 64, "selection_sha256": "b" * 64,
            "scope": "sdk_pipeline", "phase": "running", "sdk_attached": True,
            "lifecycle": {"flush": "not_started", "shutdown": "not_started"},
            "metric_flush": {"attempts": "0", "failed_attempts": "0"},
            "span_accounting": {"processors_observed": 1, "batch_processors_observed": 1,
                                "sampled_started": "0", "sampled_ended": "0", "sampled_active": "0",
                                "queue_pending": None, "queue_dropped": None, "queue_evidence": "unavailable"},
            "trace_exports": exports,
            "metric_exports": exports | {"unit": "metric_points"},
            "downstream_receipt": "unavailable", "otlp_partial_rejection": "unavailable"}


class HealthShapeTests(unittest.TestCase):
    def test_rejects_missing_malformed_and_false_delivery_claims(self) -> None:
        good = report()
        self.assertIs(health.validate(good), good)
        cases = [None, [], good | {"schema_version": True}, good | {"pid": True},
                 good | {"phase": "complete"}, good | {"policy_sha256": "stale"},
                 good | {"selection_sha256": None}, good | {"lifecycle": {}},
                 good | {"metric_flush": {}}, good | {"metric_flush": {"attempts": None, "failed_attempts": "0"}},
                 good | {"span_accounting": None}, good | {"trace_exports": []},
                 good | {"downstream_receipt": "succeeded"}]
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                health.validate(candidate)
        for section, field, value in (("span_accounting", "sampled_started", -1),
                                      ("span_accounting", "queue_pending", "00"),
                                      ("span_accounting", "queue_dropped", "-1"),
                                      ("span_accounting", "processors_observed", True),
                                      ("trace_exports", "units_started", 0),
                                      ("metric_exports", "unit", "observations")):
            candidate = copy.deepcopy(good)
            candidate[section][field] = value
            with self.subTest(section=section, field=field), self.assertRaises(ValueError):
                health.validate(candidate)
        for section, field in (("span_accounting", "queue_pending"), ("trace_exports", "batches_started")):
            candidate = copy.deepcopy(good)
            del candidate[section][field]
            with self.assertRaises(ValueError):
                health.validate(candidate)

    def test_private_report_reader_refuses_links_public_and_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps(report()))
            path.chmod(0o600)
            self.assertEqual(health.read(path)["pid"], os.getpid())
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                health.read(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                health.read(path)
            path.chmod(0o600)
            path.write_bytes(b"x" * 4097)
            with self.assertRaises(ValueError):
                health.read(path)


class JavaHealthTests(unittest.TestCase):
    def test_same_pid_metrics_off_health_and_final_otlp_receipt(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        data = copy.deepcopy(Policy.load(Path("examples/otelc.toml")).data)
        data["lifetimes"]["enabled"] = False
        data["traces"]["sample_ratio"] = 1.
        data["metrics"].update(collection=False, emission=False)
        data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
        with tempfile.TemporaryDirectory(prefix="otelc-health-", dir="/tmp") as directory:
            runtime = Path(directory)
            command = java.command(Policy.parse(data), runtime)
            environment = os.environ | {"OTEL_SDK_DISABLED": "true", "OTEL_JAVAAGENT_ENABLED": "false"}
            with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=environment) as process:
                try:
                    socket = runtime / "control.sock"
                    control.wait_ready(socket, process.pid)
                    self.assertEqual(batch(process, 2)["calls"], 2)
                    value = control.request(socket, "health")
                    self.assertEqual(value["pid"], process.pid)
                    self.assertEqual(value["span_accounting"]["sampled_ended"], "2")
                    self.assertIsNone(value["span_accounting"]["queue_dropped"])
                    self.assertEqual(value["metric_exports"]["batches_started"], "0")
                    policy_digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
                    self.assertEqual(value["policy_sha256"], policy_digest)
                    self.assertEqual(value["selection_sha256"], hashlib.sha256(
                        (runtime / "selection.json").read_bytes().rstrip(b"\n")).hexdigest())
                    control.request(socket, "on")
                    batch(process, 2)
                    process.communicate("quit\n", timeout=15)
                    self.assertEqual(process.returncode, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            final = health.read(runtime / "health.json")
            self.assertEqual(final["phase"], "final")
            self.assertEqual(final["pid"], value["pid"])
            self.assertEqual(final["lifecycle"], {"flush": "succeeded", "shutdown": "succeeded"}, json.dumps(final))
            self.assertEqual(final["span_accounting"]["queue_pending"], "0")
            self.assertEqual(final["span_accounting"]["queue_dropped"], "0")
            self.assertEqual(final["trace_exports"]["units_succeeded"], "4")
            self.assertEqual(final["trace_exports"]["units_failed"], "0")
            self.assertGreater(int(final["metric_exports"]["units_succeeded"]), 0)
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(sum("workload" in span.name for span in spans), 4,
                             "decoded receiver evidence is checked separately from SDK success")
