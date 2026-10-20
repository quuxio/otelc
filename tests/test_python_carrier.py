"""Use language-neutral expected values, including bounded malformed inputs."""
import json
import unittest
from pathlib import Path

from test_python_adapter import ADAPTER  # Establish the selected adapter import root.
from quux_otelc_python.carrier import Context, extract, from_sdk, inject
from opentelemetry.trace import INVALID_SPAN_CONTEXT, SpanContext, TraceFlags, TraceState


class PythonCarrierTests(unittest.TestCase):
    def test_shared_language_vectors_do_not_log_header_payloads(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/trace-context.json').read_text())
        self.assertEqual(1, fixture['schema_version'])
        self.assertEqual(len(fixture['cases']), len({case['name'] for case in fixture['cases']}))
        for case in fixture['cases']:
            with self.subTest(case=case['name']), self.assertNoLogs('opentelemetry.trace.span', level='WARNING'):
                context = extract(case['traceparent'], case['tracestate'])
                expected = case['expected']
                if expected is None:
                    self.assertIsNone(context)
                    continue
                actual_trace_id = f'{context.trace_id:032x}'
                actual_parent_id = f'{context.span_id:016x}'
                self.assertEqual(expected['trace_id'], actual_trace_id)
                self.assertEqual(expected['parent_id'], actual_parent_id)
                self.assertEqual(expected['flags'], int(context.trace_flags))
                outgoing = {'traceparent': f"00-{expected['trace_id']}-{expected['parent_id']}-{expected['flags']:02x}"}
                if expected['tracestate'] is not None:
                    outgoing['tracestate'] = expected['tracestate']
                self.assertEqual(outgoing, inject(context))
                self.assertEqual(outgoing, inject(extract([outgoing['traceparent']], [outgoing.get('tracestate', '')])))

    def test_invalid_python_values_and_sdk_contexts_are_not_stringified(self):
        self.assertTrue(ADAPTER.is_dir())
        for parents in ([None], [1], ['01-' + 'x' * 52 + '\ud800']):
            self.assertIsNone(extract(parents))
        valid = '00-' + '1' * 32 + '-' + '2' * 16 + '-01'
        self.assertFalse(extract([valid], [None]).tracestate)
        self.assertIsNone(from_sdk(INVALID_SPAN_CONTEXT))
        self.assertIsNone(from_sdk(None))
        self.assertIsNone(from_sdk(SpanContext(1, 2, False, TraceFlags(256))))
        self.assertIsNone(from_sdk(SpanContext(1, 2, False, TraceFlags(1), object())))

    def test_invalid_parent_never_reads_state_or_dispatches_string_hooks(self):
        class Opaque:
            def __len__(self):
                raise AssertionError('state was inspected')

        class Hooked(str):
            def encode(self, *_args, **_kwargs):
                raise AssertionError('string hook was invoked')

        self.assertIsNone(extract(['invalid'], Opaque()))
        valid = '00-' + '1' * 32 + '-' + '2' * 16 + '-01'
        self.assertIsNone(extract([Hooked(valid)]))
        self.assertFalse(extract([valid], [Hooked('a=1')]).tracestate)

    def test_sdk_state_is_bounded_and_randomness_is_preserved(self):
        pairs = [(f'key{i:02}', 'v' * 16) for i in range(32)]
        context = SpanContext(1, 2, False, TraceFlags(0xff), TraceState(pairs))
        result = inject(from_sdk(context))
        self.assertTrue(result['traceparent'].endswith('-03'))
        self.assertEqual(','.join(f'{key}={value}' for key, value in pairs[:22]), result['tracestate'])

    def test_public_context_cannot_emit_invalid_identity_or_state(self):
        for arguments in ((0, 2, 1, None), (1 << 128, 2, 1, None),
                          (1, 0, 1, None), (1, 1 << 64, 1, None),
                          (True, 2, 1, None), (1, 2, True, None),
                          (1, 2, 4, None), (1, 2, 1, ''),
                          (1, 2, 1, 'a=1,a=2'), (1, 2, 1, object()),
                          (1, 2, 1, 'a=' + 'x' * 257)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Context(*arguments)
        self.assertEqual({'traceparent': '00-' + '0' * 31 + '1-' + '0' * 15 + '2-00'},
                         inject(Context(1, 2, 0, None)))
