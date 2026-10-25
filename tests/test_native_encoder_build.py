"""The fixed helper generator rejects drift without executing module inputs."""

from __future__ import annotations

import hashlib
import tempfile
import types
from pathlib import Path
import unittest
from unittest import mock

from ci import native_encoder

ROOT = Path(__file__).resolve().parents[1]


class NativeEncoderBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (ROOT / 'otelc/python_code_descriptor.py').read_bytes()

    def test_generation_is_deterministic_and_pins_actual_dependency_bytes(self) -> None:
        header, inputs = native_encoder.generate(ROOT)
        self.assertEqual(native_encoder.generate(ROOT), (header, inputs))
        self.assertEqual(hashlib.sha256(header).hexdigest(), inputs['header_sha256'])
        self.assertEqual(hashlib.sha256(self.source).hexdigest(), inputs['source_sha256'])
        for name in ('json', 'struct'):
            path = Path(inputs[name]['path'])
            self.assertEqual(path.resolve(strict=True), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), inputs[name]['sha256'])
        self.assertIn(b'PyUnstable_Code_NewWithPosOnlyArgs', header)

    def test_helper_module_is_not_executed(self) -> None:
        source = self.source + b'\nraise AssertionError("module execution is forbidden")\n'
        self.assertTrue(native_encoder.header(source))

    def test_source_limit_and_missing_or_ambiguous_encoder_refuse(self) -> None:
        sources = (b'x' * (native_encoder.MAX_SOURCE + 1), b'value = 1\n',
                   b'def encode(): pass\ndef encode(): pass\n',
                   b'async def encode(value): return value\n',
                   b'def encode(value): yield value\n', b'class encode: pass\n')
        for source in sources:
            with self.subTest(size=len(source)), self.assertRaises(ValueError):
                native_encoder.load(source)

    def test_changed_defaults_and_runtime_global_resolution_refuse(self) -> None:
        changed = self.source.replace(b'_hash=object.__hash__', b'_hash=hash')
        with self.assertRaisesRegex(ValueError, 'defaults changed'):
            native_encoder.header(changed)
        required = self.source.replace(b'_hash=object.__hash__', b'_hash')
        with self.assertRaisesRegex(ValueError, 'defaults changed'):
            native_encoder.header(required)
        for source in (b'def encode(value): return print(value)\n',
                       b'def encode(value):\n import os\n return value\n'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'runtime globals or imports'):
                native_encoder.header(source)

    def test_constant_budget_and_unknown_types_refuse(self) -> None:
        with mock.patch.object(native_encoder, 'MAX_NODES', 1):
            factory = native_encoder.Factory()
            with self.assertRaisesRegex(ValueError, 'constant count'):
                factory.node((1, 2))
        factory = native_encoder.Factory()
        with self.assertRaisesRegex(ValueError, 'unsupported trusted encoder constant'):
            factory.node(object())

    def test_shared_constants_keep_one_owned_identity(self) -> None:
        value = ('shared',)
        factory = native_encoder.Factory()
        first = factory.node(value)
        self.assertEqual(factory.node(value), first)
        self.assertIs(factory.seen[id(value)][0], value)
        self.assertIn('PyTuple_New(1)', '\n'.join(factory.rows))

    def test_builtin_dependencies_pin_the_actual_shared_interpreter_container(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / 'libpython.dylib'
            library.write_bytes(b'owned interpreter container')
            with mock.patch.object(native_encoder._imp, 'is_builtin', return_value=1), \
                    mock.patch.object(native_encoder.sysconfig, 'get_config_var',
                                      side_effect=lambda key: {'LIBDIR': temporary, 'LDLIBRARY': library.name}[key]):
                for name in ('_json', '_struct'):
                    self.assertEqual(native_encoder.dependency_library(name, types.ModuleType(name)), library.resolve())
                library.unlink()
                prepared_input = types.ModuleType('_json')
                with self.assertRaises(FileNotFoundError):
                    native_encoder.dependency_library('_json', prepared_input)

    def test_missing_file_requires_a_real_builtin_and_available_shared_library_metadata(self) -> None:
        module = types.ModuleType('_json')
        with mock.patch.object(native_encoder._imp, 'is_builtin', return_value=0), \
                self.assertRaisesRegex(ValueError, 'no library origin'):
            native_encoder.dependency_library('_json', module)
        with self.assertRaisesRegex(ValueError, 'no library origin'):
            native_encoder.dependency_library('unknown', module)
        with mock.patch.object(native_encoder._imp, 'is_builtin', return_value=1), \
                mock.patch.object(native_encoder.sysconfig, 'get_config_var', return_value=None), \
                self.assertRaisesRegex(ValueError, 'shared Python library'):
            native_encoder.dependency_library('_json', module)
