"""Owned SDK process: drain actual pipe EOF before reporting final evidence."""

from __future__ import annotations

# The private SDK interpreter uses -I/-S/-B and this exact script. Explicit
# source/wheel paths avoid application PYTHONPATH and installed startup hooks.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from otelc.cli_bootstrap import install_sdk_paths
install_sdk_paths()

import argparse
import hashlib
import json
import os
import re
import stat
import threading
from typing import Any

from otelc.config import Policy
from otelc.python_handoff import HandoffReader
from otelc import python_sdk_io as io
from otelc import python_sdk_protocol as wire
from otelc.python_sdk_pipeline import SdkPipeline


def checkpoint(value: Any, pid: int, launch_id: str,
               received_before: int) -> dict[str, Any]:
    """Bind final probe counts to this application and the actual input stream."""
    wire.fields(value, {'pid', 'launch_id', 'function_completions',
                        'active_functions', 'runtime_losses', 'handoff',
                        'span_observation'}, {'lifetimes'})
    if value['pid'] != pid or value['launch_id'] != launch_id:
        raise ValueError('Python SDK checkpoint identity differs')
    for key in ('pid', 'function_completions', 'active_functions'):
        wire.integer(value[key])
    losses = value['runtime_losses']
    if type(losses) is not dict or len(losses) > 32:
        raise ValueError('invalid Python runtime losses')
    for key, count in losses.items():
        wire.text(key, limit=128)
        wire.integer(count)
    handoff = wire.fields(value['handoff'], {'attempted', 'accepted', 'lost', 'deadline_ms'})
    for key in ('attempted', 'accepted', 'lost'):
        wire.integer(handoff[key])
    if (handoff['accepted'] != received_before
            or handoff['attempted'] != handoff['accepted'] + handoff['lost']
            or wire.number(handoff['deadline_ms']) < 0):
        raise ValueError('Python checkpoint handoff counts differ')
    spans = wire.fields(value['span_observation'],
                        {'sampled_started', 'sampled_ended', 'handed_off'})
    for count in spans.values():
        wire.integer(count)
    if not spans['handed_off'] <= spans['sampled_ended'] <= spans['sampled_started']:
        raise ValueError('invalid Python span observation counts')
    if 'lifetimes' in value:
        lifetime_counts(value['lifetimes'])
    return value


def lifetime_counts(value: Any) -> None:
    """Keep uncompleted GC/resource observations explicitly censored at EOF."""
    names = {'admitted', 'gc_completed', 'resource_completed', 'current_live',
             'open_resources', 'tracked_instances', 'tracked_constructors',
             'failed_initialisations', 'failed_releases', 'censored', 'lost'}
    wire.fields(value, names | {'losses'})
    for key in names:
        wire.integer(value[key])
    if type(value['losses']) is not dict or len(value['losses']) > 32:
        raise ValueError('invalid Python lifetime losses')
    for key, count in value['losses'].items():
        wire.text(key, limit=128)
        wire.integer(count)
    if value['lost'] != sum(value['losses'].values()):
        raise ValueError('Python lifetime loss counts differ')


class Worker:
    """Consume records serially while SDK operations run off the input thread."""

    def __init__(self, plan: dict[str, Any], replies: int) -> None:
        wire.fields(plan, {'schema_version', 'launch_id', 'policy', 'directory'})
        if plan['schema_version'] != 1 or type(plan['schema_version']) is not int:
            raise ValueError('unsupported Python SDK plan')
        self.launch_id = wire.text(plan['launch_id'], limit=32)
        if not re.fullmatch('[0-9a-f]{32}', self.launch_id):
            raise ValueError('invalid Python SDK launch identity')
        self.policy = Policy.parse(plan['policy'])
        self.policy_sha256 = hashlib.sha256(json.dumps(
            self.policy.data, sort_keys=True, separators=(',', ':'),
        ).encode()).hexdigest()
        if 'python' not in self.policy.data['languages']:
            raise ValueError('Python is disabled by policy')
        self.directory = Path(wire.text(plan['directory'])).resolve()
        info = self.directory.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('Python SDK runtime directory must be owned and private')
        self.replies = replies
        self.pipeline: SdkPipeline | None = None
        self.pid: int | None = None
        self.checkpoint: dict[str, Any] | None = None
        self.records = 0
        self.protocol_errors = self.operation_failures = self.reply_lost = 0
        self._flush_thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        state = self.pipeline.snapshot() if self.pipeline else {
            'phase': 'waiting_application',
            'collection': self.policy.data['metrics']['collection'],
            'emission': self.policy.data['metrics']['emission'],
        }
        return {**state, 'schema_version': 1, 'kind': 'otelc.python.sdk',
                'sdk_pid': os.getpid(), 'pid': self.pid,
                'launch_id': self.launch_id, 'handoff_received': self.records,
                'policy_sha256': self.policy_sha256,
                'operation_pending': bool(self._flush_thread and self._flush_thread.is_alive()),
                'operation_failures': self.operation_failures,
                'protocol_errors': self.protocol_errors, 'reply_lost': self.reply_lost}

    def respond(self, identity: int, ok: bool) -> None:
        if not io.reply(self.replies, {'id': identity, 'ok': ok, 'status': self.status()}):
            self.reply_lost += 1

    def consume(self, record: dict[str, Any]) -> None:
        self.records += 1
        if self.checkpoint is not None:
            raise ValueError('Python SDK record follows the final checkpoint')
        kind = record.get('kind')
        if self.pipeline is None:
            self._hello(record)
        elif kind == 'metric':
            self.pipeline.metric(record)
        elif kind == 'span':
            wire.fields(record, {'kind', 'span'})
            self.pipeline.span(record['span'])
        elif kind == 'controls':
            wire.fields(record, {'kind', 'collection', 'emission'})
            self.pipeline.controls.update(collection=wire.flag(record['collection']),
                                          emission=wire.flag(record['emission']))
        elif kind == 'request':
            self._request(record)
        elif kind == 'checkpoint':
            wire.fields(record, {'kind', 'status'})
            self.checkpoint = checkpoint(record['status'], self.pid,
                                         self.launch_id, self.records - 1)
        else:
            raise ValueError('unknown Python SDK record')

    def _hello(self, record: dict[str, Any]) -> None:
        wire.fields(record, {'kind', 'id', 'pid', 'launch_id'})
        if record['kind'] != 'hello' or record['launch_id'] != self.launch_id:
            raise ValueError('Python SDK application identity differs')
        self.pid = wire.integer(record['pid'], minimum=1)
        self.pipeline = SdkPipeline(self.policy, self.launch_id, self.pid)
        self.respond(wire.integer(record['id'], minimum=1), True)

    def _request(self, record: dict[str, Any]) -> None:
        wire.fields(record, {'kind', 'id', 'command'})
        identity = wire.integer(record['id'], maximum=2 ** 32 - 1, minimum=1)
        if record['command'] not in ('status', 'flush'):
            raise ValueError('unsupported Python SDK request')
        ok = record['command'] == 'status' or self._flush()
        self.respond(identity, ok)

    def _flush(self) -> bool:
        if self._flush_thread and self._flush_thread.is_alive():
            self.operation_failures += 1
            return False
        result = []
        done = threading.Event()

        def run() -> None:
            succeeded = False
            try:
                self.pipeline.flush()
                succeeded = True
            except Exception:
                pass
            finally:
                result.append(succeeded)
                done.set()

        self._flush_thread = threading.Thread(target=run, daemon=True)
        self._flush_thread.start()
        budget = min(self.policy.data['export']['timeout_ms'] * 2 + 100,
                     self.policy.data['export']['shutdown_timeout_ms'])
        ok = done.wait(budget / 1000) and result == [True]
        if not ok:
            self.operation_failures += 1
        return ok

    def finish(self, reader: HandoffReader) -> dict[str, Any]:
        """Stop owned SDK work after actual EOF; never manufacture completions."""
        completed = self._shutdown()
        state = self.status()
        checkpoint = self.checkpoint
        good = bool(completed and checkpoint and not reader.lost
                    and not self.protocol_errors and not self.operation_failures)
        if checkpoint:
            good = self._final_counts(checkpoint, state) and good
        return {**state, 'sdk_phase': state['phase'],
                'phase': state['phase'] if good else 'failed',
                'producer_checkpoint_available': checkpoint is not None,
                'producer': checkpoint, 'handoff_incomplete': reader.lost,
                'shutdown_completed': completed}

    def _shutdown(self) -> bool:
        if self.pipeline is None:
            return False
        done = threading.Event()
        result = []

        def run() -> None:
            succeeded = False
            try:
                self.pipeline.shutdown()
                succeeded = True
            except Exception:
                pass
            finally:
                result.append(succeeded)
                done.set()

        threading.Thread(target=run, daemon=True).start()
        return (done.wait(self.policy.data['export']['shutdown_timeout_ms'] / 1000 + .1)
                and result == [True])

    def _final_counts(self, checkpoint: dict[str, Any],
                      state: dict[str, Any]) -> bool:
        losses = checkpoint['runtime_losses']
        losses['incomplete'] = losses.get('incomplete', 0) + checkpoint['active_functions']
        lifetimes = checkpoint.get('lifetimes')
        if lifetimes:
            lifetimes['censored'] = lifetimes['current_live']
            lifetimes['tracked_instances'] = lifetimes['tracked_constructors'] = 0
        spans = checkpoint['span_observation']
        counts = state['span_accounting']
        return (not any(losses.values()) and not checkpoint['handoff']['lost']
                and (not lifetimes or not lifetimes['lost'])
                and spans['sampled_started'] == spans['sampled_ended']
                and spans['handed_off'] == counts['received']
                and counts['queue_evidence'] == 'derived_after_shutdown'
                and counts['queue_dropped'] == 0
                and state['export_failed'] == state['in_flight_exports'] == 0
                and state['flush_failed'] == state['in_flight_flushes'] == 0
                and state['traces']['failed'] == state['traces']['pending'] == 0
                and state['metric_collection']['failed'] == state['metric_collection']['pending'] == 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ('plan', 'events', 'replies'):
        parser.add_argument('--' + name + '-fd', type=int, required=True)
    args = parser.parse_args()
    for descriptor in (args.events_fd, args.replies_fd):
        if not stat.S_ISFIFO(os.fstat(descriptor).st_mode):
            raise ValueError('Python SDK requires inherited pipes')
        os.set_inheritable(descriptor, False)
    os.set_blocking(args.replies_fd, False)
    worker = Worker(io.inherited_plan(args.plan_fd), args.replies_fd)
    output = io.create_file(worker.directory / 'python-sdk-status.json')
    reader = HandoffReader()
    try:
        io.publish(worker.directory / 'python-sdk-ready.json', {
            'schema_version': 1, 'pid': os.getpid(), 'launch_id': worker.launch_id,
        })
        try:
            while chunk := os.read(args.events_fd, 65536):
                for record in reader.push(chunk):
                    worker.consume(record)
        except Exception:
            # This is the owned SDK boundary, including SDK registration and
            # encoding failures whose concrete exception types are not fixed.
            worker.protocol_errors += 1
        finally:
            reader.finish()
            os.close(args.events_fd)
        final = worker.finish(reader)
        io.write_json(output, final)
        return 0 if final['phase'] == 'closed' else 1
    finally:
        os.close(output)
        os.close(args.replies_fd)


if __name__ == '__main__':
    raise SystemExit(main())
