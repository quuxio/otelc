"""A real GC callback can overlap the SDK control record in either direction."""

from __future__ import annotations

import gc
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from otelc.config import Policy
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkProcess
from test_java_integration import Capture
import test_python_lifetime_components as lifetime_fixture


class PythonLifetimeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        policy = Policy.parse({'schema_version': 3,
            'sources': {'include': ['test_python_lifetime_components.py']},
            'lifetimes': {'enabled': True, 'include': ['*']},
            'traces': {'enabled': False},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 100000}})
        self.sdk = SdkProcess.start(policy, Path(self.temporary.name).resolve() / 'sdk')
        self.addCleanup(self.sdk.abort)
        self.runtime = PythonRuntime(policy, self.sdk.channel, root=Path(__file__).resolve().parent)
        self.addCleanup(self.runtime.close)
        self.helper = lifetime_fixture.PythonLifetimeComponentTests()
        self.helper.runtime = self.runtime
        self.helper.tracker = self.runtime.lifetimes
        item = lifetime_fixture.Item()
        self.helper.admit(item)
        self.holder = [item]

    def finish(self, *, completion: bool, failed: bool = False) -> None:
        self.runtime.close()
        report = self.sdk.wait_report()
        self.assertEqual(report['phase'], 'failed' if failed else 'closed')
        counts = report['producer']['lifetimes']
        self.assertEqual((counts['admitted'], counts['gc_completed'], counts['lost']), (1, 1, 0))
        self.assertEqual({point.as_int for point in self.capture.points('otelc.lifetime.completed')},
                         {1} if completion else set())
        if not completion:
            self.assertEqual(self.capture.points('otelc.lifetime.duration'), [])

    def drop_during_control(self, collection: bool) -> list[bool]:
        send = self.sdk.channel.writer.send
        observed = []

        def wrapped(record):
            if record.get('kind') == 'controls' and record['collection'] is collection:
                done = threading.Event()

                def collect():
                    self.holder.clear()
                    gc.collect()
                    done.set()

                # The control caller owns the runtime lock. The GC worker
                # must finish while the caller also owns the SDK writer lock.
                with self.sdk.channel.writer._lock:
                    worker = threading.Thread(target=collect, daemon=True)
                    worker.start()
                    self.assertTrue(done.wait(2), 'GC callback waited for an SDK/runtime lock')
                    worker.join(2)
                    observed.extend(event.collect for event in self.runtime.lifetimes._pending
                                    if hasattr(event, 'collect'))
            return send(record)

        with mock.patch.object(self.sdk.channel.writer, 'send', side_effect=wrapped):
            self.runtime.controls(collection, True)
        return observed

    def test_gc_before_off_is_published_before_the_control_record(self) -> None:
        self.holder.clear()
        gc.collect()
        self.runtime.controls(False, True)
        self.runtime.controls(True, True)
        self.finish(completion=True)

    def test_gc_during_off_handoff_belongs_to_the_new_disabled_interval(self) -> None:
        self.assertEqual(self.drop_during_control(False), [False])
        self.runtime.controls(True, True)
        self.finish(completion=False)

    def test_gc_during_on_handoff_is_published_after_the_enable_record(self) -> None:
        self.runtime.controls(False, True)
        self.assertEqual(self.drop_during_control(True), [True])
        self.finish(completion=True)

    def test_failed_control_resets_the_boundary_and_retains_failure(self) -> None:
        send = self.sdk.channel.writer.send

        def fail_control(record):
            return False if record.get('kind') == 'controls' else send(record)

        with mock.patch.object(self.sdk.channel.writer, 'send', side_effect=fail_control):
            with self.assertRaisesRegex(RuntimeError, 'control handoff failed'):
                self.runtime.controls(False, True)
        self.assertTrue(self.runtime.collection)
        self.assertIsNone(self.runtime.lifetimes._control_collection)
        self.holder.clear()
        gc.collect()
        self.finish(completion=True, failed=True)
