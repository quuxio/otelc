"""The common client drives a real Node SDK and independently decodes its OTLP."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import control
from otelc.config import Policy
from test_java_integration import Capture, wait_for_count

ROOT = Path(__file__).resolve().parent.parent
NODE = Path('/opt/homebrew/opt/node@24/bin/node')


def node_command() -> str:
    """Use the explicit supported local toolchain, or CI's selected Node 24+."""
    if os.environ.get("OTELC_TEST_NODE"):
        return os.environ["OTELC_TEST_NODE"]
    return str(NODE) if NODE.is_file() and not os.environ.get("CI") else "node"


class NodeSdkTests(unittest.TestCase):
    def test_same_pid_common_controls_cached_instruments_callbacks_and_decoded_signals(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        fixture = ROOT / 'adapters/node/test/fixtures/pipeline.ts'
        original = fixture.read_bytes()
        with tempfile.TemporaryDirectory(prefix='otelc-node-', dir='/tmp') as directory:
            runtime = Path(directory).resolve()
            policy = Policy.parse({'schema_version': 3, 'languages': ['javascript', 'typescript'],
                'metrics': {'collection': False, 'emission': False},
                'traces': {'enabled': True, 'sample_ratio': 1.0},
                'export': {'endpoint': f'http://127.0.0.1:{capture.server.server_port}'}})
            policy_file = runtime / 'policy.json'
            policy_file.write_text(json.dumps(policy.data))
            with subprocess.Popen([node_command(), str(fixture), str(policy_file), str(runtime)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                idle = None
                try:
                    endpoint = runtime / 'control.json'
                    ready = control.wait_ready(endpoint, process.pid)
                    self.assertFalse(ready['collection'])
                    self.assertFalse(ready['emission'])
                    for collection, emission, expected in ((False, False, 0), (True, True, 2),
                                                         (True, False, 4), (False, True, 4), (True, True, 6)):
                        control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                        state = control.request(endpoint, 'emission ' + ('on' if emission else 'off'))
                        self.assertEqual(state['pid'], process.pid)
                        before = len(capture.metrics)
                        process.stdin.write('callbacks\n')
                        process.stdin.flush()
                        callbacks = json.loads(process.stdout.readline())['callback_calls']
                        process.stdin.write('2\n')
                        process.stdin.flush()
                        self.assertEqual(json.loads(process.stdout.readline()), {'checksum': 2, 'pid': process.pid})
                        state = control.request(endpoint, 'flush')
                        self.assertEqual(state['export_failed'], 0)
                        if collection and emission:
                            self.assertEqual(wait_for_count(capture.calls, expected), expected)
                        else:
                            self.assertEqual(len(capture.metrics), before)
                        if not collection:
                            process.stdin.write('callbacks\n')
                            process.stdin.flush()
                            self.assertEqual(json.loads(process.stdout.readline())['callback_calls'], callbacks)
                    process.stdin.write('linked\n')
                    process.stdin.flush()
                    self.assertEqual(json.loads(process.stdout.readline()), {'linked': True})
                    control.request(endpoint, 'flush')
                    descriptor = control.endpoint_descriptor(endpoint, endpoint.stat())
                    idle = socket.create_connection((descriptor['host'], descriptor['port']), timeout=1)
                    stdout, stderr = process.communicate('quit\n', timeout=10)
                    self.assertEqual((process.returncode, stdout, stderr), (0, '', ''))
                    self.assertFalse(endpoint.exists())
                finally:
                    if idle is not None:
                        idle.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            self.assertEqual(fixture.read_bytes(), original)
            status = json.loads((runtime / 'sdk-status.json').read_text())
            self.assertEqual(status['phase'], 'closed')
            self.assertEqual(status['traces']['exported_items'], 6)
            self.assertEqual(status['traces']['failed'], 0)
            self.assertEqual(status['in_flight_exports'], 0)
            self.assertEqual(capture.calls(), 6)
            for resource in ([item.resource for message in capture.metrics for item in message.resource_metrics]
                             + [item.resource for message in capture.traces for item in message.resource_spans]):
                attributes = {attribute.key: attribute.value.string_value for attribute in resource.attributes}
                self.assertEqual(attributes['service.name'], policy.data['resource']['service_name'])
                self.assertEqual(attributes['service.instance.id'], str(process.pid))
                self.assertEqual(attributes['telemetry.sdk.name'], 'opentelemetry')
                self.assertEqual(attributes['telemetry.sdk.language'], 'nodejs')
                self.assertEqual(attributes['telemetry.sdk.version'], '2.12.0')
            spans = [span for message in capture.traces for resource in message.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 6)
            self.assertEqual(sum(span.name == 'SDK fixture batch' for span in spans), 5)
            linked = next(span for span in spans if span.name == 'SDK fixture linked')
            self.assertEqual(linked.trace_id.hex(), '1' * 32)
            self.assertEqual(linked.parent_span_id.hex(), '2' * 16)
            self.assertEqual(linked.trace_state, 'vendor=kept')
            self.assertEqual(linked.links[0].trace_id.hex(), '3' * 32)
            self.assertEqual(linked.links[0].span_id.hex(), '4' * 16)
            self.assertEqual(linked.links[0].trace_state, 'other=kept')
            self.assertEqual(linked.events[0].name, 'fixture event')
            self.assertGreater(linked.events[0].time_unix_nano, linked.start_time_unix_nano)
            self.assertEqual(linked.status.code, 2)
            self.assertEqual(linked.status.message, 'controlled fixture status')
            self.assertGreaterEqual(linked.end_time_unix_nano, linked.events[0].time_unix_nano)
            points = capture.points('otelc.function.duration')
            self.assertTrue(points)
            self.assertEqual(list(points[-1].explicit_bounds), policy.data['metrics']['histogram_boundaries_seconds'])
