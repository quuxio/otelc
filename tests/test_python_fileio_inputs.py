"""Raw FileLoader reads retain actual bytes without changing application files."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from otelc import python_input_journal as journal
from python_input_support import NativeInputCase


class PythonFileIOInputTests(NativeInputCase):
    maxDiff = None

    def run_pair(self, source: str) -> journal.InputJournal:
        entry = self.root / 'entry.py'
        entry.write_text(source)
        arguments = ['-B', str(entry)]
        plain = subprocess.run([sys.executable, *arguments], cwd=self.root, capture_output=True, timeout=15)
        status, out, err, path, pid = self.launch(arguments)
        self.assertEqual((status, out, err), (plain.returncode, plain.stdout, plain.stderr))
        self.assertEqual(status, 0, err)
        self.assertEqual(entry.read_text(), source)
        return journal.read(path, expected_pid=pid)

    def observed(self, path: Path, evidence: journal.InputJournal) -> list:
        return [item for item in evidence.imports if os.fsdecode(item.path) == str(path)]

    def test_raw_loader_and_ordinary_application_file_metadata_remain_original(self) -> None:
        path = self.root / 'data.bin'
        path.write_bytes(b'raw\x00\xff')
        evidence = self.run_pair('''import _io
from importlib._bootstrap_external import FileLoader
with _io.FileIO("data.bin", "r") as file:
    print(type(file) is _io.FileIO, type(file.read).__name__, file.read.__self__ is file)
    print(file.name, file.mode, file.seekable(), file.read())
print(FileLoader("data", "data.bin").get_data("data.bin"))
''')
        self.assertTrue(evidence.complete)
        rows = [item for item in evidence.imports if item.path == b'data.bin']
        self.assertEqual([item.content for item in rows], [path.read_bytes()])
        info = path.stat()
        self.assertEqual((rows[0].device, rows[0].inode, rows[0].mtime_ns, rows[0].mode),
                         (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_mode))

    def test_raw_bytes_paths_and_repeated_reads_keep_actual_records(self) -> None:
        path = self.root / 'data-\u00e9'
        path.write_bytes(b'abc')
        evidence = self.run_pair('''from importlib._bootstrap_external import FileLoader
path = b'data-\\xc3\\xa9'
for index in range(64):
    assert FileLoader("data", path).get_data(path) == b"abc"
print("repeated", index + 1)
''')
        self.assertTrue(evidence.complete)
        rows = [item for item in evidence.imports if item.path == b'data-\xc3\xa9']
        self.assertEqual([item.content for item in rows], [b'abc'] * 64)

    def test_profile_callbacks_keep_original_loader_frames_and_file_collection(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''import _io, sys, weakref
from importlib._bootstrap_external import FileLoader
original = FileLoader.get_data.__code__
references, events = [], []
def profile(frame, event, function):
    if event == "c_call" and getattr(function, "__name__", None) == "read":
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO:
            assert frame.f_code is original
            references.append(weakref.ref(file))
            events.append((frame.f_code.co_name, function.__name__, type(file).__name__))
sys.setprofile(profile)
assert FileLoader("data", "data.bin").get_data("data.bin") == b"abc"
sys.setprofile(None)
assert events == [("get_data", "read", "FileIO")], events
assert references and all(reference() is None for reference in references)
assert FileLoader.get_data.__code__ is original
print("original frames and file lifetime")
''')
        self.assertTrue(evidence.complete)

    def test_profile_read_method_equality_hash_and_cached_descriptor_are_preserved(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''import _io, sys
from importlib._bootstrap_external import FileLoader
descriptor = _io.FileIO.read
events = []
def profile(frame, event, function):
    if event == "c_call" and getattr(function, "__name__", None) == "read":
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO:
            expected = descriptor.__get__(file, _io.FileIO)
            events.append((function == expected, hash(function) == hash(expected)))
sys.setprofile(profile)
for _ in range(32):
    assert FileLoader("data", "data.bin").get_data("data.bin") == b"abc"
sys.setprofile(None)
assert events == [(True, True)] * 32, events
assert _io.FileIO.read is descriptor
with _io.FileIO("data.bin", "r") as file:
    method = file.read
    assert method == descriptor.__get__(file, _io.FileIO)
    assert method(1) == b"a" and descriptor(file, 1) == b"b"
    try: method(size=1)
    except TypeError as error: print(type(error).__name__, error.args)
    else: raise AssertionError("positional-only read argument became a keyword")
print("original descriptor, bound equality/hash and literal read arguments")
''')
        self.assertTrue(evidence.complete)
        self.assertEqual([item.content for item in evidence.imports if item.path == b'data.bin'], [b'abc'] * 32)

    def test_original_closed_file_exception_and_cleanup_are_preserved(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''import _io, sys
from importlib._bootstrap_external import FileLoader
def profile(frame, event, function):
    if event == "c_call" and getattr(function, "__name__", None) == "read":
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO: file.close()
sys.setprofile(profile)
try:
    FileLoader("data", "data.bin").get_data("data.bin")
except ValueError as error:
    sys.setprofile(None)
    print(type(error).__name__, error.args)
else: raise AssertionError("closed-file exception was lost")
''')
        self.assertFalse(evidence.complete)
        self.assertGreater(evidence.capture_failures, 0)

    def test_pathlike_inputs_keep_one_original_callback_and_report_unavailable_path(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''from importlib._bootstrap_external import FileLoader
calls = 0
class Path:
    def __fspath__(self):
        global calls
        calls += 1
        return "data.bin"
path = Path()
assert FileLoader("data", "data.bin").get_data(path) == b"abc"
assert calls == 1
print("path callbacks", calls)
''')
        self.assertFalse(evidence.complete)
        self.assertGreater(evidence.capture_failures, 0)

    def test_subclass_read_replacement_remains_application_owned(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''import _io
from importlib._bootstrap_external import FileLoader
calls = 0
class ApplicationFile(_io.FileIO):
    def read(self, *args, **kwargs):
        global calls
        calls += 1
        return b"application"
_io.FileIO = ApplicationFile
assert FileLoader("data", "data.bin").get_data("data.bin") == b"application"
assert calls == 1
print("application read", calls)
''')
        # Entry completeness is not import closure. Custom classes/factories
        # remain outside this ordinary raw-file profile, with no fabricated row.
        self.assertEqual([item for item in evidence.imports if item.path == b'data.bin'], [])

    def test_reinitialisation_and_fd_reuse_cannot_replay_a_loader_constructor(self) -> None:
        (self.root / 'original.bin').write_bytes(b'original')
        (self.root / 'replacement.bin').write_bytes(b'replacement')
        for replacement in ('file.__init__("replacement.bin", "r")',
                            'descriptor = os.open("replacement.bin", os.O_RDONLY)\n'
                            '            os.dup2(descriptor, file.fileno())\n'
                            '            os.close(descriptor)'):
            source = '''import _io, os, sys
from importlib._bootstrap_external import FileLoader
changed = False
def profile(frame, event, function):
    global changed
    if event == "c_call" and getattr(function, "__name__", None) == "read" and not changed:
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO:
            changed = True
            REPLACE
sys.setprofile(profile)
result = FileLoader("data", "original.bin").get_data("original.bin")
sys.setprofile(None)
assert result == b"replacement"
print("original replacement behaviour", result)
'''.replace('REPLACE', replacement)
            with self.subTest(replacement=replacement):
                evidence = self.run_pair(source)
                self.assertFalse(evidence.complete)
                self.assertGreater(evidence.capture_failures, 0)
                self.assertEqual([item for item in evidence.imports if item.path == b'original.bin'], [])

    def test_nested_loader_reads_do_not_share_constructor_or_duplicate_suppression(self) -> None:
        (self.root / 'outer.bin').write_bytes(b'outer')
        (self.root / 'inner.bin').write_bytes(b'inner')
        sources = [
            '''import _io, sys
from importlib._bootstrap_external import FileLoader
nested = []
def profile(frame, event, function):
    if event == "c_call" and getattr(function, "__name__", None) == "read" and not nested:
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO:
            nested.append(FileLoader("inner", "inner.bin").get_data("inner.bin"))
sys.setprofile(profile)
value = FileLoader("outer", "outer.bin").get_data("outer.bin")
sys.setprofile(None)
assert nested == [b"inner"] and value == b"outer"
print(nested, value)
''',
            '''import sys
from importlib._bootstrap_external import FileLoader, SourceFileLoader
nested = []
def audit(event, args):
    if event == "open" and args[0] == "outer.bin" and not nested:
        nested.append(FileLoader("inner", "inner.bin").get_data("inner.bin"))
sys.addaudithook(audit)
value = SourceFileLoader("outer", "outer.bin").get_data("outer.bin")
assert nested == [b"inner"] and value == b"outer"
print(nested, value)
''']
        for source in sources:
            with self.subTest(source=source):
                evidence = self.run_pair(source)
                self.assertTrue(evidence.complete)
                rows = [(item.path, item.content) for item in evidence.imports
                        if item.path in (b'outer.bin', b'inner.bin')]
                self.assertEqual(rows, [(b'inner.bin', b'inner'), (b'outer.bin', b'outer')])

    def test_oversized_and_nonregular_raw_reads_preserve_values_with_capture_loss(self) -> None:
        path = self.root / 'large.bin'
        path.write_bytes(b'x' * (journal.MAX_SOURCE + 1))
        evidence = self.run_pair('''from importlib._bootstrap_external import FileLoader
print(len(FileLoader("large", "large.bin").get_data("large.bin")))
assert FileLoader("empty", "/dev/null").get_data("/dev/null") == b""
''')
        self.assertFalse(evidence.complete)
        self.assertGreaterEqual(evidence.capture_failures, 2)
        self.assertEqual(self.observed(path, evidence), [])

    def test_concurrent_raw_and_open_code_reads_keep_thread_local_ownership(self) -> None:
        for index in range(4):
            (self.root / f'thread-{index}.bin').write_bytes(str(index).encode() * 1024)
        evidence = self.run_pair('''from concurrent.futures import ThreadPoolExecutor
from importlib._bootstrap_external import FileLoader, SourceFileLoader
def read(index):
    path = "thread-%s.bin" % index
    loader = (FileLoader, SourceFileLoader)[index % 2]("thread", path)
    for _ in range(16):
        assert loader.get_data(path) == str(index).encode() * 1024
    return index
with ThreadPoolExecutor(max_workers=4) as workers:
    assert list(workers.map(read, range(4))) == list(range(4))
print("four threads, sixty-four original reads")
''')
        self.assertTrue(evidence.complete)
        for index in range(4):
            path = f'thread-{index}.bin'.encode()
            rows = [item for item in evidence.imports if item.path == path]
            self.assertEqual([item.content for item in rows], [str(index).encode() * 1024] * 16)

    def test_live_file_capacity_does_not_retain_files_or_change_reads(self) -> None:
        (self.root / 'data.bin').write_bytes(b'abc')
        evidence = self.run_pair('''import _io, sys, weakref
from importlib._bootstrap_external import FileLoader
retained = []
def profile(frame, event, function):
    if event == "c_call" and getattr(function, "__name__", None) == "read":
        file = getattr(function, "__self__", None)
        if type(file) is _io.FileIO:
            original = _io.FileIO.read.__get__(file, _io.FileIO)
            actual = (type(function), function.__name__, function.__qualname__, function.__doc__, function.__text_signature__)
            expected = (type(original), original.__name__, original.__qualname__, original.__doc__, original.__text_signature__)
            assert actual == expected, (actual, expected)
            retained.append(file)
sys.setprofile(profile)
for index in range(4097):
    assert FileLoader("data", "data.bin").get_data("data.bin") == b"abc"
sys.setprofile(None)
assert len(retained) == 4097 and all(file.closed for file in retained)
references = [weakref.ref(file) for file in retained]
retained.clear()
assert all(reference() is None for reference in references)
print("bounded capture; all reads and file lifetimes preserved")
''')
        self.assertFalse(evidence.complete)
        self.assertGreater(evidence.capture_failures, 0)
        rows = [item for item in evidence.imports if item.path == b'data.bin']
        # The shared 4096-record journal also reserves entry/final records and
        # can reach its limit before the separate live-file table is full.
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 4094)
        self.assertTrue(all(item.content == b'abc' for item in rows))
