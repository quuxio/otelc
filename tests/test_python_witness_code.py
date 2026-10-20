"""Qualify actual code identity and passive metadata, without claiming a runtime profile."""
import gc
import sys
import unittest
import weakref
from unittest.mock import patch

from test_python_adapter import ROOT
from quux_otelc_python import witness_code as binding


class PythonWitnessCodeTests(unittest.TestCase):
    def code(self):
        return compile('def action(value):\n    return value + 1\n', 'original.py', 'exec')

    def test_actual_compiler_code_is_stable_but_structural_clone_cannot_satisfy_lookup(self):
        self.assertTrue(ROOT.is_dir())
        code = self.code()
        clone = code.replace()
        self.assertIsNot(code, clone)
        self.assertEqual(code, clone)  # This is precisely why code-keyed dictionaries are unsafe.
        registry = binding.CodeRegistry()
        handle = registry.register(code)
        self.assertEqual(handle.digest, binding.fingerprint(clone))
        self.assertIs(registry.lookup(code), handle)
        self.assertIs(registry.register(code), handle)
        self.assertIsNone(registry.lookup(clone))
        other = registry.register(clone)
        self.assertEqual(other.digest, handle.digest)
        self.assertIsNot(other, handle)
        self.assertIs(registry.lookup(clone), other)
        self.assertIsNone(registry.lookup(object()))

    def test_registry_does_not_retain_original_code_and_close_revokes_handles(self):
        registry = binding.CodeRegistry(capacity=1)
        code = self.code()
        reference = weakref.ref(code)
        registry.register(code)
        del code
        gc.collect()
        self.assertIsNone(reference())
        self.assertFalse(registry.entries)
        replacement = self.code()
        registry.register(replacement)
        with self.assertRaisesRegex(binding.CodeBindingError, 'code_registry_capacity'):
            registry.register(replacement.replace())
        registry.close()
        self.assertIsNone(registry.lookup(replacement))
        with self.assertRaisesRegex(binding.CodeBindingError, 'code_registry_closed'):
            registry.register(replacement)
        for capacity in (True, 0, binding.MAX_NODES + 1):
            with self.subTest(capacity=capacity), self.assertRaises(binding.CodeBindingError):
                binding.CodeRegistry(capacity)

    def test_unknown_constant_hooks_are_never_called_even_when_refusing(self):
        class Hostile:
            def __repr__(self):
                raise AssertionError('repr was dispatched')

            def __hash__(self):
                raise AssertionError('hash was dispatched')

            def __eq__(self, other):
                raise AssertionError('equality was dispatched')

        code = self.code().replace(co_consts=(Hostile(),))
        with self.assertRaisesRegex(binding.CodeBindingError, 'unsupported_code_constant'):
            binding.fingerprint(code)
        self.assertIsNone(binding.CodeRegistry().lookup(code))

    def test_constant_metaclass_equality_is_not_used_for_type_selection(self):
        class HookedType(type):
            def __eq__(cls, other):
                raise AssertionError('metaclass equality was dispatched')

        class Opaque(metaclass=HookedType):
            pass

        code = self.code().replace(co_consts=(Opaque(),))
        with self.assertRaisesRegex(binding.CodeBindingError, 'unsupported_code_constant'):
            binding.fingerprint(code)

    def test_fingerprint_binds_original_code_positions_closure_and_constants(self):
        code = self.code()
        original = binding.fingerprint(code)
        for changes in ({'co_filename': 'other.py'}, {'co_firstlineno': 99},
                        {'co_consts': (42,)}, {'co_code': b'\x09\x00' + code.co_code},
                        {'co_freevars': ('captured',)}, {'co_qualname': 'other'},
                        {'co_exceptiontable': b'changed'}, {'co_linetable': b'changed'}):
            modified = code.replace(**changes)
            with self.subTest(fields=list(changes)):
                self.assertNotEqual(original, binding.fingerprint(modified))
        for constant in (None, Ellipsis, False, True, 0, -1, 1.5, -0.0, 1 + 2j,
                         'text\ud800', b'bytes', (1, 'two'), frozenset(('one', 'two'))):
            modified = code.replace(co_consts=(constant,))
            with self.subTest(kind=type(constant).__name__):
                self.assertEqual(binding.fingerprint(modified), binding.fingerprint(modified.replace()))

    def test_limits_and_unsupported_compiler_profiles_refuse_explicitly(self):
        code = self.code()
        for constant, reason in ((1 << 4097, 'integer_capacity'),
                                 (b'x' * binding.MAX_BYTES, 'code_byte_capacity'),
                                 ('x' * (binding.MAX_BYTES + 1), 'code_byte_capacity'),
                                 (tuple(range(binding.MAX_NODES + 1)), 'code_node_capacity')):
            modified = code.replace(co_consts=(constant,))
            with self.subTest(reason=reason), self.assertRaisesRegex(binding.CodeBindingError, reason):
                binding.fingerprint(modified)
        deep = None
        for _ in range(binding.MAX_DEPTH + 1):
            deep = (deep,)
        modified = code.replace(co_consts=(deep,))
        with self.assertRaisesRegex(binding.CodeBindingError, 'code_node_capacity'):
            binding.fingerprint(modified)
        for version in ((3, 11, 0), (3, 13, 0), (3, 15, 0)):
            with patch.object(sys, 'version_info', version), self.assertRaisesRegex(binding.CodeBindingError, 'unsupported_compiler_profile'):
                binding.fingerprint(code)
        with self.assertRaisesRegex(binding.CodeBindingError, 'unsupported_compiler_profile'):
            binding.fingerprint(object())
