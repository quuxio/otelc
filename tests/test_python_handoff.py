"""Actual pipes qualify bounded, recoverable Python observation handoff."""

from __future__ import annotations

import concurrent.futures
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

from otelc.python_handoff import (
    HEADER, MAGIC, MAX_ID, MAX_RECORD, PAYLOAD, HandoffReader, HandoffWriter,
)


def frame(data: bytes, *, identity: int = 1, part: int = 0,
          total: int = 1) -> bytes:
    return HEADER.pack(MAGIC, identity, part, total, len(data)) + data


def available(descriptor: int) -> bytes:
    """Drain only current bytes, without depending on writer EOF."""
    chunks = []
    os.set_blocking(descriptor, False)
    while True:
        try:
            data = os.read(descriptor, 65536)
        except BlockingIOError:
            break
        if not data:
            break
        chunks.append(data)
    return b''.join(chunks)


class PythonHandoffTests(unittest.TestCase):
    def pipe(self, **kwargs):
        reader, descriptor = os.pipe()
        self.addCleanup(os.close, reader)
        writer = HandoffWriter(descriptor, **kwargs)
        self.addCleanup(writer.close)
        return reader, writer

    def test_real_pipe_preserves_unicode_primitives_and_fragmented_records(self) -> None:
        descriptor, writer = self.pipe()
        first = {'kind': 'metric', 'name': 'α😀', 'value': 2 ** 60,
                 'attributes': {'enabled': True, 'other': None},
                 'items': ['a', 1.25, False], 'padding': 'x' * 2000}
        second = {'kind': 'span', 'id': '0011223344556677'}
        before = random.getstate()
        self.assertTrue(writer.send(first))
        self.assertTrue(writer.send(second))
        self.assertEqual(random.getstate(), before)
        self.assertFalse(os.get_inheritable(writer.descriptor))
        self.assertFalse(os.get_blocking(writer.descriptor))
        data = available(descriptor)
        decoder = HandoffReader()
        records = []
        for byte in data:
            records.extend(decoder.push(bytes([byte])))
        decoder.finish()
        self.assertEqual(records, [first, second])
        self.assertEqual((decoder.accepted, decoder.lost), (2, 0))
        self.assertEqual(writer.snapshot(),
                         {'attempted': 2, 'accepted': 2, 'lost': 0,
                          'deadline_ms': 50.})

    def test_blocked_actual_pipe_is_bounded_and_next_record_recovers(self) -> None:
        descriptor, pipe = os.pipe()
        writer = HandoffWriter(pipe, timeout_ms=5)
        open_reader = True
        try:
            with concurrent.futures.ThreadPoolExecutor() as pool:
                started = time.monotonic()
                pending = pool.submit(writer.send, {'data': 'x' * (2 * 1024 * 1024)})
                try:
                    self.assertFalse(pending.result(timeout=2))
                    self.assertLess(time.monotonic() - started, 2)
                finally:
                    if not pending.done():
                        os.close(descriptor)
                        open_reader = False
            decoder = HandoffReader()
            self.assertEqual(decoder.push(available(descriptor)), [])
            self.assertTrue(writer.send({'kind': 'recovered'}))
            self.assertEqual(decoder.push(available(descriptor)),
                             [{'kind': 'recovered'}])
            decoder.finish()
            self.assertEqual((decoder.accepted, decoder.lost), (1, 1))
            self.assertEqual(writer.snapshot()['lost'], 1)
            self.assertEqual(writer.snapshot()['accepted'], 1)
        finally:
            if open_reader:
                os.close(descriptor)
            writer.close()

    def test_reader_death_and_closed_writer_do_not_throw_into_the_probe(self) -> None:
        descriptor, pipe = os.pipe()
        writer = HandoffWriter(pipe)
        os.close(descriptor)
        self.assertFalse(writer.send({'kind': 'lost'}))
        writer.close()
        writer.close()
        self.assertFalse(writer.send({'kind': 'closed'}))
        self.assertEqual(writer.snapshot()['lost'], 2)
        self.assertEqual(writer.snapshot()['accepted'], 0)

    def test_dead_reader_preserves_default_custom_and_blocked_sigpipe_state(self) -> None:
        code = '''import json,os,signal,sys
from otelc.python_handoff import HandoffWriter
mode=sys.argv[1];calls=[]
handler=signal.SIG_DFL if mode=='default' else lambda *args:calls.append(1)
signal.signal(signal.SIGPIPE,handler)
if mode.startswith('blocked'):
    signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGPIPE})
if mode=='blocked-pending':
    signal.raise_signal(signal.SIGPIPE)
before=signal.pthread_sigmask(signal.SIG_BLOCK,set())
reader,fd=os.pipe();writer=HandoffWriter(fd);os.close(reader)
accepted=writer.send({'kind':'dead-reader'})
after=signal.pthread_sigmask(signal.SIG_BLOCK,set())
pending=signal.SIGPIPE in signal.sigpending()
writer.close()
print(json.dumps({'accepted':accepted,'handler_unchanged':signal.getsignal(signal.SIGPIPE)==handler,
 'mask_unchanged':before==after,'pending':pending,'handler_calls':len(calls)}))
'''
        for mode in ('default', 'custom', 'blocked', 'blocked-pending'):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-c', code, mode],
                    capture_output=True, text=True, timeout=5)
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                self.assertEqual(json.loads(result.stdout), {
                    'accepted': False, 'handler_unchanged': True,
                    'mask_unchanged': True, 'pending': mode == 'blocked-pending',
                    'handler_calls': 0,
                })

    def test_eof_is_final_and_counts_a_partial_record_once(self) -> None:
        for data in (frame(b'{}')[:7], frame(b'{"kind":', total=2)):
            with self.subTest(data=data):
                decoder = HandoffReader()
                self.assertEqual(decoder.push(data), [])
                decoder.finish()
                decoder.finish()
                self.assertEqual((decoder.accepted, decoder.lost), (0, 1))
                data = frame(b'{}', identity=2)
                with self.assertRaisesRegex(ValueError, 'finished'):
                    decoder.push(data)

    def test_malformed_framing_and_sequences_are_refused(self) -> None:
        invalid = (
            HEADER.pack(0, 1, 0, 1, 2) + b'{}',
            HEADER.pack(MAGIC, 0, 0, 1, 2) + b'{}',
            HEADER.pack(MAGIC, 1, 0, 0, 2) + b'{}',
            HEADER.pack(MAGIC, 1, 1, 1, 2) + b'{}',
            HEADER.pack(MAGIC, 1, 0, 1, 0),
            HEADER.pack(MAGIC, 1, 0, 1, PAYLOAD + 1),
            frame(b'{}', part=1, total=2),
            frame(b'{', total=2) + frame(b'}', part=0, total=2),
            frame(b'{', total=2) + frame(b'}', part=1, total=3),
            frame(b'{}', identity=2) + frame(b'{}', identity=1),
            frame(b'{}') + frame(b'{}'),
        )
        for data in invalid:
            decoder = HandoffReader()
            with self.subTest(data=data), self.assertRaises(ValueError):
                decoder.push(data)

    def test_ambiguous_nonfinite_nonobject_or_invalid_json_is_not_telemetry(self) -> None:
        for value in (b'{"key":1,"key":2}', b'{"v":NaN}',
                      b'{"v":Infinity}', b'{"v":1e999}', b'[]',
                      b'null', b'{', b'{"v":"\xff"}'):
            with self.subTest(value=value):
                decoder = HandoffReader()
                data = frame(value)
                with self.assertRaises(ValueError):
                    decoder.push(data)
                decoder.finish()
                self.assertEqual((decoder.accepted, decoder.lost), (0, 1))

    def test_record_encoding_failures_and_capacity_are_loss_without_output(self) -> None:
        descriptor, writer = self.pipe()
        cyclic = {}
        cyclic['self'] = cyclic
        for record in ({'value': float('nan')}, {'value': object()}, cyclic,
                       {'value': 'x' * MAX_RECORD}, ['not', 'an', 'object']):
            with self.subTest(kind=type(record).__name__):
                self.assertFalse(writer.send(record))
        self.assertEqual(available(descriptor), b'')
        self.assertEqual(writer.snapshot()['lost'], 5)
        self.assertEqual(writer.snapshot()['accepted'], 0)

    def test_identity_exhaustion_never_wraps_and_corrupts_a_later_record(self) -> None:
        descriptor, writer = self.pipe()
        writer._next = MAX_ID
        self.assertTrue(writer.send({'kind': 'last'}))
        self.assertFalse(writer.send({'kind': 'wrapped'}))
        decoder = HandoffReader()
        self.assertEqual(decoder.push(available(descriptor)), [{'kind': 'last'}])
        self.assertEqual(writer.snapshot()['lost'], 1)
        self.assertEqual(decoder.accepted, 1)

    def test_only_pipe_owners_and_finite_nonnegative_deadlines_are_supported(self) -> None:
        with tempfile.TemporaryFile() as regular:
            descriptor = regular.fileno()
            with self.assertRaisesRegex(ValueError, 'requires a pipe'):
                HandoffWriter(descriptor)
            regular.write(b'caller still owns this file')
        for timeout in (-1, float('inf'), float('nan')):
            reader, descriptor = os.pipe()
            try:
                with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                    HandoffWriter(descriptor, timeout_ms=timeout)
            finally:
                os.close(reader)
                os.close(descriptor)

    def test_concurrent_real_pipe_records_keep_complete_boundaries(self) -> None:
        descriptor, writer = self.pipe()
        records = [{'id': index, 'value': 'α' * 20} for index in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(writer.send, records)))
        decoder = HandoffReader()
        received = decoder.push(available(descriptor))
        self.assertEqual(sorted(received, key=lambda row: row['id']), records)
        decoder.finish()
        self.assertEqual((decoder.accepted, decoder.lost), (12, 0))
