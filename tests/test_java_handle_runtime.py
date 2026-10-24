"""Actual private JDK handle operands bind fresh diagnostic scopes without source edits."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import java, witness
import test_witness as direct


def source(receiver: bool) -> str:
    text = direct.RECEIVER_SOURCE if receiver else direct.SOURCE
    text = text.replace('import java.io.*;', 'import java.io.*;\nimport java.lang.invoke.*;')
    factory = f'''static final MethodHandle HANDLE = handle();
  static MethodHandle handle() {{
    try {{ return MethodHandles.privateLookupIn(Work.class,MethodHandles.lookup()).
      {'findVirtual' if receiver else 'findStatic'}(Work.class,"target",MethodType.methodType(int.class,int.class)); }}
    catch (ReflectiveOperationException failure) {{ throw new AssertionError("handle fixture setup failed"); }}
  }}
  '''
    text = text.replace('static final BufferedReader INPUT', factory + 'static final BufferedReader INPUT')
    old = ('static int wrapper(Work receiver, int seed) { return receiver.target(seed); }' if receiver else
           'static int wrapper(int seed) { return Work.target(seed); }')
    new = ('static int wrapper(Work receiver, int seed) throws Throwable { return (int) HANDLE.invokeExact(receiver,seed); }'
           if receiver else 'static int wrapper(int seed) throws Throwable { return (int) HANDLE.invokeExact(seed); }')
    text = text.replace(old, new)
    text = text.replace('throws IOException { rendezvous();', 'throws Throwable { rendezvous();')
    text = text.replace('main(String[] args) throws IOException', 'main(String[] args) throws Throwable')
    text = text.replace('int target(int seed)', 'private int target(int seed)') if receiver else text.replace(
        'static int target(int seed)', 'private static int target(int seed)')
    return text


def wide_source(receiver: bool) -> str:
    """Exercise both category-two arguments through the actual verifier and probe."""
    text = source(receiver).replace('int seed)', 'long seed,double fraction)')
    text = text.replace('MethodType.methodType(int.class,int.class)',
                        'MethodType.methodType(int.class,long.class,double.class)')
    text = text.replace('HANDLE.invokeExact(receiver,seed)', 'HANDLE.invokeExact(receiver,seed,fraction)')
    text = text.replace('HANDLE.invokeExact(seed)', 'HANDLE.invokeExact(seed,fraction)')
    text = text.replace('return wrapper(receiver, seed);', 'return wrapper(receiver, seed,fraction);')
    text = text.replace('return wrapper(seed);', 'return wrapper(seed,fraction);')
    text = text.replace('envelope(receiver, Integer.parseInt(line))',
                        'envelope(receiver, Long.parseLong(line),Double.parseDouble(line)+0.25)')
    text = text.replace('envelope(Integer.parseInt(line))',
                        'envelope(Long.parseLong(line),Double.parseDouble(line)+0.25)')
    text = text.replace('return seed + offset;', 'return Math.toIntExact(seed + offset);')
    text = text.replace('return seed + 1;', 'return Math.toIntExact(seed + 1);')
    text = text.replace('if (seed < 0) throw FAILURE;',
        'if(Double.doubleToRawLongBits(fraction)!=Double.doubleToRawLongBits((double)seed+0.25))'
        'throw new AssertionError("wide argument changed"); if (seed < 0) throw FAILURE;')
    return text


class JavaHandleRuntimeTests(unittest.TestCase):
    def test_wide_fixture_refuses_a_changed_double_even_when_integer_result_would_match(self) -> None:
        text = wide_source(False).replace('Double.parseDouble(line)+0.25', 'Double.parseDouble(line)+0.5')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / 'Harness.java'
            entry.write_text(text)
            subprocess.run([java.executable('javac'), '-d', str(root), str(entry)],
                           capture_output=True, check=True, timeout=20)
            result = subprocess.run([java.executable('java'), '-cp', str(root), 'Harness'],
                                    input=b'7\ngo\nquit\n', capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'wide argument changed', result.stderr)

    def test_actual_static_long_double_arguments_preserve_values_and_receipt(self) -> None:
        direct.WitnessIntegrationTests.exercise(self, wide_source(False), profile=witness.HANDLE_PROFILE,
                                               argument_descriptor='JD')

    def test_actual_instance_long_double_arguments_preserve_values_live_metrics_and_receipt(self) -> None:
        direct.WitnessIntegrationTests.exercise(self, wide_source(True), receiver=True,
            profile=witness.HANDLE_PROFILE, live_metrics=True, argument_descriptor='JD')

    def test_private_static_handle_preserves_scopes_errors_and_actual_receipt(self) -> None:
        direct.WitnessIntegrationTests.exercise(self, source(False), profile=witness.HANDLE_PROFILE)

    def test_private_instance_handle_preserves_state_live_metrics_and_receipt(self) -> None:
        direct.WitnessIntegrationTests.exercise(self, source(True), receiver=True,
                                               profile=witness.HANDLE_PROFILE, live_metrics=True)

    def test_same_output_from_wrong_direct_handle_cannot_complete(self) -> None:
        text = source(False).replace('findStatic(Work.class,"target",', 'findStatic(Work.class,"other",')
        text = text.replace('static int getters;', 'static int getters;\n  private static int other(int seed) { return seed+1; }')
        direct.WitnessIntegrationTests.exercise(self, text, profile=witness.HANDLE_PROFILE,
            substituted=True, expected_refusal='handle_identity_mismatch', substituted_value=8)

    def test_adapted_same_type_handle_cannot_borrow_the_original_target(self) -> None:
        text = source(False).replace('try { return MethodHandles.privateLookupIn', 'try { MethodHandle original = MethodHandles.privateLookupIn')
        text = text.replace('MethodType.methodType(int.class,int.class)); }',
            'MethodType.methodType(int.class,int.class)); return MethodHandles.filterReturnValue(original, '
            'MethodHandles.lookup().findStatic(Harness.class,"identity",MethodType.methodType(int.class,int.class))); }')
        text = text.replace('static final BufferedReader INPUT', 'static int identity(int value) { return value; }\n  static final BufferedReader INPUT')
        direct.WitnessIntegrationTests.exercise(self, text, profile=witness.HANDLE_PROFILE,
            substituted=True, expected_refusal='handle_identity_mismatch', substituted_value=8, extra_unscoped=1)

    def test_bound_handle_restored_to_the_original_type_is_refused(self) -> None:
        text = source(True).replace('try { return MethodHandles.privateLookupIn', 'try { MethodHandle original = MethodHandles.privateLookupIn')
        text = text.replace('MethodType.methodType(int.class,int.class)); }',
            'MethodType.methodType(int.class,int.class)); return MethodHandles.dropArguments(original.bindTo(new Work()),0,Work.class); }')
        direct.WitnessIntegrationTests.exercise(self, text, receiver=True, profile=witness.HANDLE_PROFILE,
            substituted=True, expected_refusal='handle_identity_mismatch', substituted_value=8, extra_unscoped=1)

    def test_subclass_receiver_cannot_substitute_for_the_exact_concrete_class(self) -> None:
        text = source(True).replace('Work receiver = new Work();', 'Work receiver = new DifferentWork();')
        text += '\nfinal class DifferentWork extends Work {}\n'
        direct.WitnessIntegrationTests.exercise(self, text, receiver=True, profile=witness.HANDLE_PROFILE,
            substituted=True, expected_refusal='unverified_handle_route', substituted_value=8, extra_unscoped=1)

    def test_target_class_initialiser_call_is_unrelated_to_the_armed_action(self) -> None:
        text = source(False).replace('static int getters;', 'static int getters;\n  static int initialiser = target(41);')
        direct.WitnessIntegrationTests.exercise(self, text, profile=witness.HANDLE_PROFILE, extra_unscoped=1)

    def test_public_and_final_instance_targets_share_the_exact_receiver_route(self) -> None:
        for access in ('public', 'public final'):
            with self.subTest(access=access):
                text = source(True).replace('private int target(int seed)', access + ' int target(int seed)')
                direct.WitnessIntegrationTests.exercise(self, text, receiver=True, profile=witness.HANDLE_PROFILE)

    def test_application_helper_collision_keeps_the_operand_observer_private(self) -> None:
        direct.WitnessIntegrationTests.exercise(self, source(False), profile=witness.HANDLE_PROFILE,
                                               helper_collision=True)

    def test_direct_private_recursion_retains_each_target_and_function_span(self) -> None:
        text = source(False).replace('if (seed < 0) throw FAILURE; return seed + 1;',
            'if (seed < 0) throw FAILURE; if(seed>5)return 1+target(seed-1); return seed + 1;')
        direct.WitnessIntegrationTests.exercise(self, text, profile=witness.HANDLE_PROFILE, recursive_entries=3)
