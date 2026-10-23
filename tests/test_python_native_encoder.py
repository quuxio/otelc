"""SDK-independent native encoding preserves applications and import contexts."""

from __future__ import annotations

import json
import os
import copy
import hashlib
import shutil
import subprocess
import sys
import tempfile
from unittest import mock
import types
from pathlib import Path

from ci import build_python_inputs as tooling
from ci import native_encoder
from otelc.python_code_descriptor import encode
from python_input_support import NativeInputCase


class PythonNativeEncoderTests(NativeInputCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(__file__).resolve().parents[1]
        cls.encoder_fixture = root / 'build/native' / ('python-encoder-fixture-' + sys.implementation.cache_tag)
        _, flags = tooling.toolchain()
        library = 'libotelc_python_inputs' + ('.dylib' if sys.platform == 'darwin' else '.so')
        coverage = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
        with tempfile.TemporaryDirectory(dir=root / 'build/native') as temporary:
            candidate = Path(temporary) / 'encoder'
            subprocess.run([tooling.compiler(), str(root / 'tests/fixtures/python_encoder.c'),
                str(cls.tool.parent / library), '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-I' + str(root / 'native/python'), '-I' + str(cls.tool.parent),
                '-Wl,-rpath,' + str(cls.tool.parent), '-o', str(candidate), *coverage, *flags], check=True)
            os.replace(candidate, cls.encoder_fixture)

    def application(self, body: str) -> Path:
        entry = self.root / 'encoder.py'
        entry.write_text(body)
        return entry

    def test_plain_encoder_matches_maintained_graph_without_sdk_or_module_lookup(self) -> None:
        source = '''import sys
import _otelc_inputs as native
def workload(value=701):
    return (value, 1.5, 2j, "text", b"bytes", None, True, ...)
class Reject:
    def find_spec(self, name, path=None, target=None):
        if name in ("_json", "_struct") or name.startswith(("otelc", "opentelemetry")):
            raise AssertionError("application import lookup")
sys.meta_path.insert(0, Reject())
assert not any(name.startswith(("otelc", "opentelemetry")) for name in sys.modules)
assert "_json" not in sys.modules and "_struct" not in sys.modules
sys.stdout.buffer.write(native.encode(workload.__code__))
assert "_json" not in sys.modules and "_struct" not in sys.modules
assert workload()[0] == 701
'''
        for name in ('_json.py', '_struct.py'):
            (self.root / name).write_text('raise AssertionError("shadow application module executed")\n')
        entry = self.application(source)
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, err), (0, b''), out)
        compiled = compile(source, str(entry), 'exec', dont_inherit=True)
        target = next(value for value in compiled.co_consts
                      if type(value) is types.CodeType and value.co_name == 'workload')
        self.assertEqual(out, encode(target))
        self.assertEqual(json.loads(out)['schema_version'], 2)
        self.assertEqual(entry.read_text(), source)

    def test_encoding_adds_no_compile_marshal_frame_or_python_constructor_audits(self) -> None:
        entry = self.application('''import sys
import _otelc_inputs as native
def workload(): return 42
def guard(event, args):
    if event in ("compile", "exec", "marshal.dumps", "marshal.loads", "sys._getframe", "code.__new__", "function.__new__"):
        raise AssertionError(event)
sys.addaudithook(guard)
assert native.encode(workload.__code__).startswith(b"{")
assert workload() == 42
print("preserved")
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'preserved\n', b''))

    def test_normal_profile_does_not_install_the_independent_encoder(self) -> None:
        entry = self.application('''import _otelc_inputs as native
def workload(): return 42
try:
    native.encode(workload.__code__)
except RuntimeError:
    print("unavailable")
else:
    raise AssertionError("observer profile was not selected")
assert workload() == 42
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)])
        self.assertEqual((status, out, err), (0, b'unavailable\n', b''))

    def test_existing_application_modules_and_startup_audit_policy_are_preserved(self) -> None:
        (self.root / 'sitecustomize.py').write_text('''import sys, types
for name in ("_json", "_struct"):
    module = types.ModuleType(name)
    module.marker = "application"
    sys.modules[name] = module
def guard(event, args):
    if event in ("code.__new__", "function.__new__", "marshal.dumps", "marshal.loads"):
        raise AssertionError(event)
    if event == "compile" and args[1] == "<otelc.native.encoder>":
        raise AssertionError("observer runtime compilation")
sys.addaudithook(guard)
''')
        entry = self.application('''import sys
import _otelc_inputs as native
assert sys.modules["_json"].marker == sys.modules["_struct"].marker == "application"
def workload(): return 42
assert native.encode(workload.__code__).startswith(b"{")
assert sys.modules["_json"].marker == sys.modules["_struct"].marker == "application"
print("startup preserved")
''')
        environment = {**os.environ, 'PYTHONPATH': str(self.root)}
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True, environment=environment)
        self.assertEqual((status, out, err), (0, b'startup preserved\n', b''))

    def test_code_is_not_retained_on_success_or_failure(self) -> None:
        entry = self.application('''import types, weakref
import _otelc_inputs as native
def workload(): return 701
function = types.FunctionType(workload.__code__.replace(), {})
reference = weakref.ref(function.__code__)
assert native.encode(function.__code__)
del function
assert reference() is None
class Trap(bytearray):
    def __buffer__(self, flags): raise AssertionError("buffer callback")
    def __repr__(self): raise AssertionError("repr callback")
    def __str__(self): raise AssertionError("str callback")
trap = Trap(b"x")
function = types.FunctionType(workload.__code__.replace(co_consts=tuple(
    trap if type(value) is int and value == 701 else value for value in workload.__code__.co_consts)), {})
reference = weakref.ref(function.__code__)
try:
    native.encode(function.__code__)
except ValueError:
    pass
else:
    raise AssertionError("unsupported constant accepted")
assert function() is trap
del function
assert reference() is None
print("released")
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'released\n', b''))

    def test_noncode_oversized_and_deep_graphs_refuse_without_extra_callbacks(self) -> None:
        entry = self.application('''import _otelc_inputs as native
def workload(): return 42
for invalid in (None, "text", workload):
    try:
        native.encode(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError("non-code accepted")
deep = 1
for index in range(140): deep = (deep,)
for value in (b"x" * (16 * 1024 * 1024), deep):
    code = workload.__code__.replace(co_consts=(None, value))
    try:
        native.encode(code)
    except ValueError:
        pass
    else:
        raise AssertionError("unbounded graph accepted")
print("bounded")
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'bounded\n', b''))

    def test_changed_private_function_defaults_are_not_called(self) -> None:
        entry = self.application('''import gc, types
import _otelc_inputs as native
function = next(value for value in gc.get_objects()
                if type(value) is types.FunctionType and value.__module__ == "otelc.native.encoder")
def trap(value): raise AssertionError("changed helper called")
function.__kwdefaults__["_len"] = trap
def workload(): return 42
try:
    native.encode(workload.__code__)
except RuntimeError:
    print("changed defaults refused")
else:
    raise AssertionError("changed helper accepted")
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'changed defaults refused\n', b''))

    def test_changing_all_visible_default_dictionaries_cannot_replace_fixed_references(self) -> None:
        entry = self.application('''import gc, types
import _otelc_inputs as native
function = next(value for value in gc.get_objects()
                if type(value) is types.FunctionType and value.__module__ == "otelc.native.encoder")
original = function.__kwdefaults__["_len"]
dictionaries = [value for value in gc.get_objects()
                if type(value) is dict and value.get("_code_type") is types.CodeType
                and value.get("_len") is original]
assert dictionaries
calls = 0
def changed(value):
    global calls
    calls += 1
    return len(value)
for dictionary in dictionaries:
    dictionary["_len"] = changed
def workload(): return 701
try:
    native.encode(workload.__code__)
except RuntimeError:
    assert calls == 0, calls
    print("fixed references preserved")
else:
    raise AssertionError(("mutable reference accepted", calls))
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'fixed references preserved\n', b''))

    def test_fork_does_not_reuse_parent_encoder_owner(self) -> None:
        entry = self.application('''import os
import _otelc_inputs as native
def workload(): return 42
child = os.fork()
if child == 0:
    try:
        native.encode(workload.__code__)
    except RuntimeError:
        os._exit(0)
    os._exit(1)
_, status = os.waitpid(child, 0)
assert status == 0 and native.encode(workload.__code__)
print("fork refused")
''')
        status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (0, b'fork refused\n', b''))

    def test_dependency_origin_configuration_and_allocation_failures_are_closed(self) -> None:
        for mode in ('missing', 'symbol', 'origin', 'configuration', 'allocation', 'repeat', 'interpreter'):
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.encoder_fixture), mode, sys.executable],
                                        capture_output=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_missing_pinned_dependency_refuses_before_application_execution(self) -> None:
        generate = native_encoder.generate
        _, original = generate(Path(__file__).resolve().parents[1])
        dependency = self.root / 'encoder-json-dependency.so'
        shutil.copyfile(original['json']['path'], dependency)

        def owned_dependency(root: Path) -> tuple[bytes, dict]:
            header, inputs = generate(root)
            inputs = copy.deepcopy(inputs)
            before = ('#define OTELC_ENCODER_JSON_PATH ' + native_encoder.Factory.data(
                os.fsencode(inputs['json']['path']) + b'\0')).encode()
            after = ('#define OTELC_ENCODER_JSON_PATH ' + native_encoder.Factory.data(
                os.fsencode(dependency) + b'\0')).encode()
            self.assertEqual(header.count(before), 1)
            header = header.replace(before, after)
            inputs['json'] = {'path': str(dependency), 'sha256': hashlib.sha256(dependency.read_bytes()).hexdigest()}
            inputs['header_sha256'] = hashlib.sha256(header).hexdigest()
            return header, inputs

        with mock.patch.object(native_encoder, 'generate', side_effect=owned_dependency):
            variant = tooling.build(self.root / 'owned-dependency-build',
                                    coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')))
        dependency.unlink()
        entry = self.application('from pathlib import Path\nPath("executed").touch()\n')
        with mock.patch.object(self, 'tool', variant):
            status, out, err, _, _ = self.launch(['-B', str(entry)], origin_observer=True)
        self.assertEqual((status, out, err), (78, b'', b''))
        self.assertFalse((self.root / 'executed').exists())
