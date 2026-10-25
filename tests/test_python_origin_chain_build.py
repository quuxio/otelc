"""Native emission maps refuse unknown images and ambiguous compiler call paths."""

from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from ci import native_origin_chain as subject


def image(*, commands: bytes | None = None, count: int = 1) -> bytes:
    """One small independent Mach-O UUID fixture, with no executable code."""
    if commands is None:
        commands = struct.pack('<II', 0x1b, 24) + bytes(range(1, 17))
    return struct.pack('<8I', 0xfeedfacf, 0x0100000c, 0, 6, count, len(commands), 0, 0) + commands


def mapping(version: tuple[int, int] = (3, 12)) -> tuple[str, dict[str, str]]:
    """Declared chain fixture: unique calls, including the compiler before audit."""
    tail = ['_pyrun_file', '__PyRun_SimpleFileObject', '__PyRun_AnyFileObject'] if version == (3, 12) else [
        '__PyRun_File', '__PyRun_SimpleFile', '__PyRun_AnyFile']
    names = ['_sys_audit_tstate', '__PySys_Audit', '_run_mod', *tail,
             '_pymain_run_file_obj', '_pymain_run_file', '_Py_RunMain', '_sentinel']
    symbols = '\n'.join('%x t %s' % (0x1000 + index * 0x100, name) for index, name in enumerate(names))
    texts = {}
    for index, name in enumerate(names[:-1]):
        start = 0x1000 + index * 0x100
        text = '' if index == 0 else '%x:\t00 00 00 94\tbl\t%s\n' % (start + 0x20, names[index - 1])
        if name == '_run_mod':
            text = '%x:\t00 00 00 94\tbl\t__PyAST_Compile\n' % start + text
        texts[name] = text
    return symbols, texts


class NativeOriginChainBuildTests(unittest.TestCase):
    def test_uuid_reader_accepts_one_exact_uuid_and_refuses_malformed_commands(self) -> None:
        self.assertEqual(subject.uuid(image()), bytes(range(1, 17)))
        corrupt = [b'', image()[:31], image()[:-1], image(count=2), image(count=0),
                   image(commands=struct.pack('<II', 0x1b, 16) + bytes(8)),
                   image(commands=struct.pack('<II', 0x1b, 24) + bytes(16)),
                   image(commands=struct.pack('<II', 0x1b, 24) + bytes(range(1, 17)) * 3, count=2)]
        for offset, value in ((0, 0), (4, 0), (8, 2), (12, 1), (16, 5000), (20, 2**24), (28, 1), (36, 7)):
            data = bytearray(image())
            struct.pack_into('<I', data, offset, value)
            corrupt.append(bytes(data))
        for data in corrupt:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                subject.uuid(data)

    def test_both_profiles_derive_adjacent_sites_without_any_application(self) -> None:
        for version in ((3, 12), (3, 14)):
            symbols, texts = mapping(version)
            rows = subject.chain(symbols, texts.__getitem__, version)
            self.assertEqual(len(rows), 9)
            self.assertEqual([row['return'] for row in rows], [0] + [0x1024 + index * 0x100 for index in range(1, 9)])

    def test_missing_symbols_ambiguous_calls_bad_bounds_and_late_compile_refuse(self) -> None:
        symbols, texts = mapping()
        mutations = [('', texts), (symbols.replace('_run_mod', '_missing'), texts),
            (symbols, {**texts, '_run_mod': texts['_run_mod'] * 2}),
            (symbols, {**texts, '_run_mod': texts['_run_mod'].replace('1220:', '12ff:')}),
            (symbols, {**texts, '_run_mod': texts['_run_mod'].replace('1200:', '1230:')}),
            (symbols, {**texts, '_run_mod': texts['_run_mod'].replace('__PyAST_Compile', '_other')})]
        for names, code in mutations:
            with self.subTest(symbols=bool(names)), self.assertRaises(ValueError):
                subject.chain(names, code.__getitem__, (3, 12))
        with self.assertRaises(ValueError):
            subject.chain(symbols, texts.__getitem__, (3, 13))
        with mock.patch.object(subject, 'MAX_SYMBOLS', 1):
            with self.assertRaises(ValueError):
                subject.chain(symbols, texts.__getitem__, (3, 12))
        with mock.patch.object(subject, 'MAX_DISASSEMBLY', 1):
            with self.assertRaises(ValueError):
                subject.chain(symbols, texts.__getitem__, (3, 12))

    def test_unavailable_profiles_and_tools_have_explicit_disabled_metadata(self) -> None:
        for platform, machine, version in [('linux', 'aarch64', (3, 12)), ('darwin', 'x86_64', (3, 12)),
                                            ('darwin', 'arm64', (3, 13))]:
            header, record = subject.prepare(Path('/not/read'), platform=platform, machine=machine,
                                             version=version, llvm=Path('/not/read'))
            self.assertEqual(header, subject.DISABLED)
            self.assertFalse(record['available'])
        with mock.patch.object(subject.shutil, 'which', return_value=None):
            header, record = subject.prepare(Path('/not/read'), platform='darwin', machine='arm64',
                                             version=(3, 12), llvm=Path('/not/read'))
        self.assertEqual(record['reason'], 'missing_tools')

    def test_preparation_pins_uuid_image_mapping_and_generated_path_bytes(self) -> None:
        symbols, texts = mapping()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / 'python-\N{GRINNING FACE}'
            library.write_bytes(image())
            (root / 'llvm-objdump').touch()
            def output(command, **kwargs):
                return symbols if len(command) == 2 else texts[command[-2]]
            with mock.patch.object(subject.shutil, 'which', return_value='/usr/bin/nm'), \
                    mock.patch.object(subject.subprocess, 'check_output', side_effect=output):
                first = subject.prepare(library, platform='darwin', machine='arm64', version=(3, 12), llvm=root)
                second = subject.prepare(library, platform='darwin', machine='arm64', version=(3, 12), llvm=root)
            self.assertEqual(first, second)
            self.assertTrue(first[1]['available'])
            self.assertEqual(first[1]['uuid'], bytes(range(1, 17)).hex())
            self.assertIn(b'240,159,152,128', first[0])

    def test_corrupt_images_disable_and_changed_image_cannot_publish_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / 'python'
            (root / 'llvm-objdump').touch()
            library.write_bytes(b'invalid')
            with mock.patch.object(subject.shutil, 'which', return_value='/usr/bin/nm'):
                header, record = subject.prepare(library, platform='darwin', machine='arm64', version=(3, 12), llvm=root)
            self.assertEqual(header, subject.DISABLED)
            self.assertEqual(record['reason'], 'unsupported_image')
            library.write_bytes(image())
            def changed(*args, **kwargs):
                library.write_bytes(image() + b'changed')
                raise OSError('tool failed after replacement')
            with mock.patch.object(subject.shutil, 'which', return_value='/usr/bin/nm'), \
                    mock.patch.object(subject.subprocess, 'check_output', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'changed during inspection'):
                    subject.prepare(library, platform='darwin', machine='arm64', version=(3, 12), llvm=root)
