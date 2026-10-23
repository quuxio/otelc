"""Original applications keep native output while exact entry bytes are retained."""

from __future__ import annotations

import os
import subprocess
import sys

from python_input_support import NativeInputCase
from otelc import python_input_journal as journal


class PythonNativeInputTests(NativeInputCase):
    def test_original_raw_channels_exits_encodings_and_main_frame_match(self) -> None:
        cases = {
            'ordinary': b'import sys\nprint("original",sys.argv[1:])\n',
            'binary': b'import sys\nsys.stdout.buffer.write(bytes([0,255,10]));sys.stderr.buffer.write(bytes([1,255,10]))\n',
            'failure': b'raise ValueError("original")\n',
            'syntax': b'if:\n',
            'exit7': b'raise SystemExit(7)\n',
            'exit_text': b'raise SystemExit("original exit")\n',
            'keyboard_interrupt': b'raise KeyboardInterrupt\n',
            'empty': b'',
            'latin1': b'# coding: latin-1\nprint("caf\xe9")\n',
            'bom': b'\xef\xbb\xbfprint("BOM")\n',
            'crlf': b'print("CRLF")\r\n',
            'no_newline': b'print("no newline")',
            'frame': b'import sys\nprint(sys._getframe().f_back is None)\n',
            'atexit': b'import atexit\natexit.register(print,"final")\nprint("main")\n',
        }
        for name, content in cases.items():
            with self.subTest(name=name):
                entry = self.root / (name + '.py')
                entry.write_bytes(content)
                native = subprocess.run([sys.executable, str(entry), 'argument'],
                    cwd=self.root, capture_output=True, timeout=15)
                status, out, err, path, pid = self.launch([str(entry), 'argument'])
                self.assertEqual((status, out, err), (native.returncode, native.stdout, native.stderr))
                evidence = journal.read(path, expected_pid=pid)
                self.assertTrue(evidence.complete)
                self.assertEqual(len(evidence.sources), 1)
                source = evidence.sources[0]
                self.assertEqual((source.path, source.content, source.mtime_ns),
                                 (os.fsencode(entry), content, entry.stat().st_mtime_ns))
                self.assertEqual(entry.read_bytes(), content)

    def test_relative_unicode_symlink_flags_and_entry_metadata_remain_native(self) -> None:
        source = b'''import json,sys
print(json.dumps({'argv':sys.argv,'orig_argv':sys.orig_argv,'path':sys.path,
'executable':sys.executable,'base_executable':sys._base_executable,
'prefix':sys.prefix,'base_prefix':sys.base_prefix,
'file':__file__,'spec':__spec__,'loader':type(__loader__).__name__,
'cached':__cached__,'back':sys._getframe().f_back is None,'flags':repr(sys.flags)},sort_keys=True))
'''
        (self.root / 'entry.py').write_bytes(source)
        (self.root / 'linked.py').symlink_to('entry.py')
        (self.root / 'caf\u00e9.py').write_bytes(source)
        for arguments in (['entry.py'], ['./entry.py'], ['linked.py'], ['caf\u00e9.py'],
                          ['-B', 'entry.py'], ['-O', 'entry.py'], ['-I', 'entry.py'], ['-S', 'entry.py']):
            with self.subTest(arguments=arguments):
                native = subprocess.run([sys.executable, *arguments, 'arg'],
                    cwd=self.root, capture_output=True, timeout=15)
                status, out, err, path, pid = self.launch([*arguments, 'arg'])
                self.assertEqual((status, out, err), (native.returncode, native.stdout, native.stderr))
                value = journal.read(path, expected_pid=pid)
                self.assertTrue(value.complete)
                self.assertEqual(value.sources[0].content, source)

    def test_source_larger_than_bound_runs_natively_but_cannot_claim_complete_evidence(self) -> None:
        content = b'print("original")\n#' + b'x' * journal.MAX_SOURCE
        entry = self.root / 'large.py'
        entry.write_bytes(content)
        status, out, err, path, pid = self.launch([str(entry)])
        self.assertEqual((status, out, err), (0, b'original\n', b''))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.complete)
        self.assertEqual((len(value.sources), value.capture_failures), (0, 1))

    def test_actual_consumed_source_is_retained_even_if_the_app_rewrites_its_file(self) -> None:
        entry = self.root / 'changing.py'
        source = b'from pathlib import Path\nPath(__file__).write_text("changed")\nprint("original")\n'
        entry.write_bytes(source)
        status, out, err, path, pid = self.launch([str(entry)])
        self.assertEqual((status, out, err), (0, b'original\n', b''))
        self.assertEqual(entry.read_text(), 'changed')
        self.assertEqual(journal.read(path, expected_pid=pid).sources[0].content, source)

    def test_snapshot_disk_failure_preserves_native_execution_and_reports_capture_loss(self) -> None:
        bootstrap = self.root / 'bootstrap'
        bootstrap.mkdir()
        (bootstrap / 'sitecustomize.py').write_text('''import resource,signal
signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE,(0,hard))
''')
        entry = self.root / 'entry.py'
        entry.write_text('''import resource
soft,hard=resource.getrlimit(resource.RLIMIT_FSIZE)
resource.setrlimit(resource.RLIMIT_FSIZE,(hard,hard))
print('original')
''')
        environment = {**os.environ, 'PYTHONPATH': str(bootstrap)}
        native = subprocess.run([sys.executable, str(entry)], cwd=self.root,
            env=environment, capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch([str(entry)], environment=environment)
        self.assertEqual((native.returncode, native.stdout, native.stderr), (0, b'original\n', b''))
        self.assertEqual((status, out, err), (native.returncode, native.stdout, native.stderr))
        value = journal.read(path, expected_pid=pid)
        self.assertFalse(value.complete)
        self.assertEqual((len(value.sources), value.capture_failures), (0, 1))

    def test_missing_file_module_and_immediate_exit_do_not_invent_entry_receipts(self) -> None:
        status, _, _, path, pid = self.launch(['missing.py'])
        self.assertEqual(status, 2)
        self.assertFalse(journal.read(path, expected_pid=pid).complete)
        (self.root / 'module.py').write_text('print("module")')
        status, out, err, path, pid = self.launch(['-m', 'module'])
        self.assertEqual((status, out, err), (0, b'module\n', b''))
        self.assertFalse(journal.read(path, expected_pid=pid).complete)
        entry = self.root / 'immediate.py'
        entry.write_text('import os\nos._exit(7)')
        status, _, _, path, pid = self.launch([str(entry)])
        self.assertEqual(status, 7)
        with self.assertRaisesRegex(ValueError, 'finalization'):
            journal.read(path, expected_pid=pid)

    def test_invalid_driver_descriptors_and_native_options_never_run_the_application(self) -> None:
        entry = self.root / 'entry.py'
        entry.write_text('print("must not run")')
        for descriptor in ('-1', 'not-an-integer', '1tail', '999999999999999999999999', '999999'):
            with self.subTest(descriptor=descriptor):
                result = subprocess.run([str(self.tool), descriptor, sys.executable,
                    self.argv0, str(entry)], capture_output=True, timeout=15)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (64, b'', b''))
        result = subprocess.run([str(self.tool)], capture_output=True, timeout=15)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (64, b'', b''))
        path = self.root / 'wrong-interpreter.journal'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            result = subprocess.run([str(self.tool), str(descriptor), sys.executable + '-other',
                self.argv0, str(entry)], capture_output=True, pass_fds=(descriptor,), timeout=15)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (64, b'', b''))
            self.assertEqual(path.read_bytes(), b'')
        finally:
            os.close(descriptor)
        arguments = ['--otelc-invalid-python-option', str(entry)]
        native = subprocess.run([sys.executable, *arguments], capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch(arguments)
        self.assertEqual((status, out, err), (native.returncode, native.stdout, native.stderr))
        self.assertFalse(journal.read(path, expected_pid=pid).complete)

    def test_native_writer_validates_ownership_bounds_counts_and_failed_io(self) -> None:
        for mode in ('ordinary', 'invalid', 'capacity', 'write-failure', 'reuse', 'file-limit', 'byte-capacity'):
            with self.subTest(mode=mode):
                path = self.root / (mode + '.journal')
                with subprocess.Popen([str(self.fixture), str(path), mode],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                    out, err = child.communicate(timeout=15)
                    self.assertEqual((child.returncode, out, err), (0, b'', b''))
                if mode in ('write-failure', 'reuse', 'file-limit'):
                    with self.assertRaises(ValueError):
                        journal.read(path, expected_pid=child.pid)
                    if mode == 'reuse':
                        self.assertEqual(path.with_name(path.name + '.foreign').read_bytes(), b'app')
                else:
                    value = journal.read(path, expected_pid=child.pid)
                    self.assertEqual(len(value.sources),
                        {'ordinary': 1, 'invalid': 0, 'capacity': 4095, 'byte-capacity': 3}[mode])
                    self.assertEqual(value.complete, mode == 'ordinary')
                    self.assertEqual(value.capture_failures,
                        {'ordinary': 0, 'invalid': 4, 'capacity': 1, 'byte-capacity': 1}[mode])

