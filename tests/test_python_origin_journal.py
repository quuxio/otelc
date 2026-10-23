"""Falsify closed origin framing and relations without running retained code."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otelc import python_input_journal as inputs
from otelc import python_origin_journal as journal
from otelc.python_code_descriptor import encode

PATH = b'entry.py'
SOURCE = b'def selected():\n    return 7\nselected()\n'
CODE = compile(SOURCE, PATH.decode(), 'exec')
CHILD_INDEX = next(index for index, value in enumerate(CODE.co_consts)
                   if type(value) is type(CODE))
CHILD = CODE.co_consts[CHILD_INDEX]


def read_record(*, pid: int = 12, path: bytes = PATH, kind: int = 1,
                content: bytes = SOURCE) -> tuple[int, bytes, bytes]:
    return kind, path, inputs.SOURCE.pack(pid, 1, 2, len(content), 0, 0,
                                         stat.S_IFREG | 0o644) + content


def root_record(*, identity=10, transaction=20, loader=0, token=1, route=1,
                version=sys.hexversion, descriptor_version=2, flags=0, optimize=0,
                content: bytes | None = None, path: bytes = PATH) -> tuple[int, bytes, bytes]:
    return 5, path, journal.ROOT.pack(12, identity, transaction, loader, token, route,
        version, descriptor_version, flags, optimize) + (encode(CODE) if content is None else content)


def execution_record(*, identity=30, root=10, offset=0, version=sys.hexversion,
                     edges=(CHILD_INDEX,), depth=None, reserved=0,
                     path: bytes = PATH) -> tuple[int, bytes, bytes]:
    payload = b''.join(journal.EDGE.pack(edge) for edge in edges)
    return 6, path, journal.EXECUTION.pack(12, identity, root, offset, version,
        len(edges) if depth is None else depth, reserved) + payload


def sdk_record(*, identity=1, offset=0, code=CHILD,
               path: bytes = PATH) -> tuple[int, bytes, bytes]:
    content = encode(code)
    return 4, path, inputs.CODE.pack(12, identity, offset, sys.hexversion, 2, len(content), 0) + content


def join_record(*, sdk=1, origin=30, offset=0, reserved=0,
                path: bytes = PATH, tail: bytes = b'') -> tuple[int, bytes, bytes]:
    return 7, path, journal.JOIN.pack(12, sdk, origin, offset, reserved, 0, 0) + tail


def wire(records: list[tuple[int, bytes, bytes]], *, lost=0, io_lost=0, finalized=1,
         expected=1, count=None, size=None) -> bytes:
    content_size = sum(len(payload) - (journal.ROOT.size if kind == 5 else inputs.SOURCE.size)
                       for kind, _, payload in records)
    final = inputs.FINAL.pack(12, len(records) if count is None else count,
        content_size if size is None else size, lost, io_lost, finalized, expected)
    return b''.join(inputs.HEADER.pack(journal.MAGIC, kind, 0, 0, sequence, len(path), len(payload))
                    + path + payload
                    for sequence, (kind, path, payload) in enumerate([*records, (2, b'', final)], 1))


class PythonOriginJournalTests(unittest.TestCase):
    def test_private_reader_keeps_version_selection_and_nonfollowing_input_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            path = Path(temporary) / 'origins.journal'
            path.write_bytes(wire([read_record(), root_record()]))
            path.chmod(0o600)
            self.assertTrue(journal.read(path, expected_pid=12).capture_complete)
            with self.assertRaises(ValueError):
                inputs.read(path, expected_pid=12)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                journal.read(path, expected_pid=12)
            path.chmod(0o600)
            link = Path(temporary) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                journal.read(link, expected_pid=12)

    def test_real_descriptors_join_only_explicit_earlier_read_root_and_execution(self) -> None:
        records = [read_record(), root_record(), execution_record(), sdk_record(), join_record()]
        value = journal.decode(wire(records), expected_pid=12)
        self.assertTrue(value.capture_complete)
        self.assertEqual(value.sources[0][0], 1)
        self.assertEqual(value.sources[0][1].content, SOURCE)
        self.assertEqual(value.roots[0].input_sequence, 1)
        self.assertEqual(value.executions[0].edges, (CHILD_INDEX,))
        self.assertEqual(value.sdk_joins, ((1, 30),))
        self.assertEqual(value.imports, ())
        prepared_input = wire(records)
        with self.assertRaises(ValueError):
            inputs.decode(prepared_input, expected_pid=12)
        prepared_input = wire(records).replace(journal.MAGIC, inputs.MAGIC)
        with self.assertRaises(ValueError):
            journal.decode(prepared_input, expected_pid=12)

    def test_identical_reads_require_distinct_tokens_transactions_and_root_ids(self) -> None:
        records = [read_record(kind=3), root_record(loader=8, route=2),
                   read_record(kind=3), root_record(identity=11, transaction=21, loader=8, token=3, route=2)]
        value = journal.decode(wire(records), expected_pid=12)
        self.assertEqual([root.input_sequence for root in value.roots], [1, 3])
        self.assertEqual([sequence for sequence, _ in value.imports], [1, 3])
        for change in ({'identity': 10}, {'transaction': 20}, {'token': 1}):
            options = dict(identity=11, transaction=21, loader=8, token=3, route=2)
            options.update(change)
            with self.subTest(change=change):
                prepared_input = wire([*records[:3], root_record(**options)])
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)

    def test_plain_execution_has_no_sdk_and_equal_unbound_code_creates_no_relation(self) -> None:
        value = journal.decode(wire([read_record(), root_record(), execution_record()]), expected_pid=12)
        self.assertTrue(value.capture_complete)
        self.assertEqual((value.codes, value.sdk_joins), ((), ()))
        unbound = journal.decode(wire([read_record(), sdk_record()]), expected_pid=12)
        self.assertEqual((unbound.roots, unbound.executions, unbound.sdk_joins), ((), (), ()))

    def test_missing_forward_or_replayed_relations_are_refused(self) -> None:
        cases = ([root_record()], [execution_record()], [join_record()],
                 [read_record(), root_record(token=2)],
                 [read_record(), root_record(), execution_record(), join_record(), sdk_record()],
                 [read_record(), root_record(), execution_record(), sdk_record(), join_record(), join_record()],
                 [read_record(), root_record(), execution_record(), execution_record(identity=31)],
                 [read_record(), root_record(), execution_record(), execution_record(edges=())])
        for records in cases:
            with self.subTest(kinds=[r[0] for r in records]):
                prepared_input = wire(records)
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)

    def test_root_route_profile_and_graph_cannot_be_forged_by_metadata(self) -> None:
        changes = ({'identity': 0}, {'transaction': 0}, {'token': 0}, {'loader': 9},
                   {'route': 0}, {'route': 2, 'loader': 8}, {'flags': 2**31}, {'optimize': 3},
                   {'path': b'other.py'}, {'version': 0x030b00f0}, {'descriptor_version': 1},
                   {'content': b'{}'}, {'content': encode(CHILD)})
        for change in changes:
            with self.subTest(change=change):
                prepared_input = wire([read_record(), root_record(**change)])
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)
        prepared_input = (wire([read_record(kind=3, path=b'entry.pyc'),
                root_record(route=2, loader=8, path=b'entry.pyc')]))
        with self.assertRaises(ValueError):
            journal.decode(prepared_input, expected_pid=12)

    def test_constant_edges_offsets_paths_versions_and_aliases_are_checked(self) -> None:
        changes = ({'identity': 0}, {'root': 0}, {'offset': 1}, {'offset': 2**32},
                   {'version': sys.hexversion + 1}, {'edges': (999,)},
                   {'edges': (len(CODE.co_consts) - 1,)}, {'depth': 0},
                   {'depth': 129}, {'reserved': 1}, {'path': b'other.py'})
        for change in changes:
            with self.subTest(change=change):
                prepared_input = wire([read_record(), root_record(), execution_record(**change)])
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)
        value = journal.decode(wire([read_record(), root_record(), execution_record(edges=())]), expected_pid=12)
        self.assertEqual(value.executions[0].node, value.roots[0].descriptor.root)
        aliased = CODE.replace(co_consts=CODE.co_consts + (CHILD,))
        prepared_input = (wire([read_record(), root_record(content=encode(aliased)),
                execution_record(), execution_record(identity=31, edges=(len(CODE.co_consts),))]))
        with self.assertRaisesRegex(ValueError, 'duplicate execution'):
            journal.decode(prepared_input, expected_pid=12)

    def test_one_process_cannot_mix_interpreter_or_filesystem_profiles(self) -> None:
        prepared_input = (wire([read_record(), root_record(), read_record(kind=3),
                root_record(identity=11, transaction=21, token=3, route=2, loader=8,
                            version=sys.hexversion + 1)]))
        with self.assertRaisesRegex(ValueError, 'mixed interpreter'):
            journal.decode(prepared_input, expected_pid=12)
        content = json.loads(encode(CODE))
        content['filesystem']['errors'] = 'strict'
        prepared_input = (wire([read_record(), root_record(), read_record(kind=3),
                root_record(identity=11, transaction=21, token=3, route=2, loader=8,
                            content=json.dumps(content).encode())]))
        with self.assertRaisesRegex(ValueError, 'mixed interpreter'):
            journal.decode(prepared_input, expected_pid=12)

    def test_sdk_join_requires_same_code_graph_offset_path_and_unique_identity(self) -> None:
        base = [read_record(), root_record(), execution_record(), sdk_record()]
        for change in ({'sdk': 2}, {'origin': 31}, {'offset': 2}, {'reserved': 1},
                       {'path': b'other.py'}, {'tail': b'junk'}):
            with self.subTest(change=change):
                prepared_input = wire([*base, join_record(**change)])
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)
        for records in ([*base, sdk_record()],
                        [*base[:3], sdk_record(code=compile('pass', 'entry.py', 'exec')), join_record()],
                        [*base[:3], sdk_record(offset=2), join_record()],
                        [*base, join_record(), sdk_record(identity=2), join_record(sdk=2)]):
            prepared_input = wire(records)
            with self.assertRaises(ValueError):
                journal.decode(prepared_input, expected_pid=12)
        with mock.patch.object(journal, 'MAX_STEPS', 1):
            prepared_input = wire([*base, join_record()])
            with self.assertRaises(ValueError):
                journal.decode(prepared_input, expected_pid=12)

    def test_losses_cannot_be_presented_as_complete_and_final_counts_cover_every_kind(self) -> None:
        records = [read_record(), root_record(), execution_record(), sdk_record(), join_record()]
        for changes in ({'lost': 1}, {'io_lost': 1}, {'finalized': 0}):
            with self.subTest(changes=changes):
                self.assertFalse(journal.decode(wire(records, **changes), expected_pid=12).capture_complete)
        for changes in ({'count': 1}, {'size': len(SOURCE)}, {'expected': 2}, {'finalized': 2}):
            with self.subTest(changes=changes):
                prepared_input = wire(records, **changes)
                with self.assertRaises(ValueError):
                    journal.decode(prepared_input, expected_pid=12)
        prepared_input = wire([sdk_record(identity=2)])
        with self.assertRaisesRegex(ValueError, 'missing SDK'):
            journal.decode(prepared_input, expected_pid=12)
        self.assertFalse(journal.decode(wire([sdk_record(identity=2)], lost=1), expected_pid=12).capture_complete)

    def test_framing_pid_bounds_unknown_kinds_and_truncation_remain_closed(self) -> None:
        good = wire([read_record()])
        header = inputs.HEADER
        invalid = [b'', good[:1], good[:-1], good + b'x', wire([read_record(pid=13)]),
                   wire([read_record(path=b'')]), wire([read_record(path=b'x\0y')]),
                   wire([(9, PATH, b'x' * 56)]),
                   header.pack(journal.MAGIC, 1, 1, 0, 1, 1, 56) + b'x' * 57,
                   header.pack(journal.MAGIC, 1, 0, 1, 1, 1, 56) + b'x' * 57,
                   header.pack(journal.MAGIC, 1, 0, 0, 2, 1, 56) + b'x' * 57,
                   header.pack(journal.MAGIC, 1, 0, 0, 1, inputs.MAX_PATH + 1, 56),
                   header.pack(journal.MAGIC, 1, 0, 0, 1, 1, inputs.MAX_SOURCE + 57),
                   good[:-header.size - inputs.FINAL.size]]
        for data in invalid:
            with self.subTest(data=data[:40]), self.assertRaises(ValueError):
                journal.decode(data, expected_pid=12)
        for data, pid in ((bytearray(good), 12), (good, 0), (good, True)):
            with self.assertRaises(ValueError):
                journal.decode(data, expected_pid=pid)
        with mock.patch.object(inputs, 'MAX_JOURNAL', 1), self.assertRaises(ValueError):
            journal.decode(good, expected_pid=12)
        bad = inputs.SOURCE.pack(12, 1, 2, 99, 0, 10**9, stat.S_IFDIR)
        prepared_input = wire([(1, PATH, bad)])
        with self.assertRaisesRegex(ValueError, 'source metadata'):
            journal.decode(prepared_input, expected_pid=12)
        for data in (wire([(4, PATH, b'x' * 56)]),
                     wire([(5, PATH, b'x' * 80)]), wire([(6, PATH, b'x' * 56)])):
            with self.assertRaises(ValueError):
                journal.decode(data, expected_pid=12)
