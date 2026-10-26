"""Cancellation must cross the child-publication boundary without losing ownership."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from otelc.go_supervisor import Owner


class GoSupervisorCancellationTests(unittest.TestCase):
    def owner(self, directory: Path) -> Owner:
        # This component fixture exercises the real child-spawn path without
        # claiming prepared-launch provenance or Go application qualification.
        owner = object.__new__(Owner)
        owner.directory = directory
        owner.original = {'plain': True, 'launch_id': '0' * 32}
        owner.plan = {'argv': [sys.executable, '-I', '-S', '-c',
                               'import time; time.sleep(30)'],
                      'cwd': str(directory), 'binary': {}}
        owner.prepared = {}
        owner.application = None
        owner.stopping = None
        owner.pending_signal = None
        return owner

    def test_cancellation_before_child_publication_reaches_and_reaps_actual_child(self) -> None:
        spawn = subprocess.Popen
        for phase in ('before-start', 'during-spawn'):
            for number in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(phase=phase, signal=number), tempfile.TemporaryDirectory() as temporary:
                    owner = self.owner(Path(temporary))

                    def interrupted_spawn(*args: object, **kwargs: object) -> subprocess.Popen:
                        application = spawn(*args, **kwargs)
                        owner.interrupt(number, None)
                        return application

                    if phase == 'before-start':
                        owner.interrupt(number, None)
                    try:
                        with patch('subprocess.Popen', side_effect=(
                                interrupted_spawn if phase == 'during-spawn' else spawn)):
                            owner.start()
                        self.assertEqual(owner.stopping, number)
                        self.assertEqual(owner.application.wait(timeout=2), -number)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(owner.application.pid, 0)
                    finally:
                        owner.cleanup()

    def test_signal_after_publication_is_forwarded_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = self.owner(Path(temporary))
            try:
                owner.start()
                with patch.object(owner.application, 'send_signal', wraps=owner.application.send_signal) as send:
                    owner.interrupt(signal.SIGTERM, None)
                    self.assertEqual(owner.application.wait(timeout=2), -signal.SIGTERM)
                send.assert_called_once_with(signal.SIGTERM)
            finally:
                owner.cleanup()

    def test_exited_child_and_exit_during_signal_do_not_break_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = self.owner(Path(temporary))
            try:
                owner.start()
                with patch.object(owner.application, 'send_signal', side_effect=ProcessLookupError):
                    owner.interrupt(signal.SIGTERM, None)
                owner.cleanup()
                owner.interrupt(signal.SIGINT, None)
                self.assertEqual(owner.stopping, signal.SIGINT)
                self.assertIsNotNone(owner.application.returncode)
            finally:
                owner.cleanup()


if __name__ == '__main__':
    unittest.main()
