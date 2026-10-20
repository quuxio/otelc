"""Model the private control lifecycle without claiming real probe attribution."""
import json
import threading
import unittest
from unittest.mock import patch

from test_python_adapter import ROOT
from quux_otelc_python.witness_state import (
    Bindings, Health, Probe, WitnessState, fresh_identity,
)
from quux_otelc_python.witness_wire import (
    MAX_INTEGER, MAX_REQUESTS, ProtocolError, decode_record, encode_record,
)


class PythonWitnessStateTests(unittest.TestCase):
    def test_frozen_declarations_activate_once_with_matching_actual_probe_handles(self):
        state = self.state(available=False)
        declared = state.bindings
        actual = Bindings(declared.build_id, declared.selection_id,
                          Probe(declared.rendezvous.digest), Probe(declared.wrapper.digest),
                          Probe(declared.target.digest), object())
        self.assertTrue(state.activate(actual))
        self.assertIs(actual, state.bindings)
        self.assertFalse(state.rendezvous(declared.rendezvous, object()))
        self.assertTrue(state.rendezvous(actual.rendezvous, object()))
        self.assertFalse(state.activate(actual))
        self.assertEqual('invalidated', state.phase)
        for wrong in (None, Bindings('0' * 64, declared.selection_id,
                                   actual.rendezvous, actual.wrapper, actual.target, object()),
                      Bindings(declared.build_id, '0' * 64,
                               actual.rendezvous, actual.wrapper, actual.target, object()),
                      Bindings(declared.build_id, declared.selection_id,
                               Probe('0' * 64), actual.wrapper, actual.target, object())):
            with self.subTest(binding=type(wrong).__name__):
                candidate = self.state(available=False)
                self.assertFalse(candidate.activate(wrong))
                self.assertFalse(candidate.available)
                self.assertEqual('wrong_binding', candidate.fault)

    def state(self, available=True):
        bindings = Bindings('a' * 64, 'b' * 64, Probe('c' * 64), Probe('d' * 64),
                            Probe('e' * 64), object())
        return WitnessState(bindings, available=available)

    def ready(self, state):
        owner = object()
        self.assertTrue(state.rendezvous(state.bindings.rendezvous, owner))
        return owner

    def arm(self, state, request_number=1, **changes):
        request = {'version': 1, 'kind': 'request', 'op': 'arm',
                   'request_id': f'{request_number:032x}', 'runtime_id': state.runtime_id,
                   'scope_id': 'f' * 32, 'context_id': state.context_id,
                   'ready_seq': state.ready_sequence,
                   'build_id': state.bindings.build_id, 'selection_id': state.bindings.selection_id,
                   'wrapper_probe_id': state.bindings.wrapper.digest,
                   'target_probe_id': state.bindings.target.digest, **changes}
        response = state.handle(encode_record(request))
        return decode_record(response) if response is not None else None

    def consume(self, state, owner):
        self.assertTrue(self.arm(state)['ok'])
        claim = state.claim_wrapper(state.bindings.wrapper, owner, state.bindings.call_site)
        self.assertIsNotNone(claim)
        self.assertTrue(state.bind_root(claim, 123, 456, sampled=True))
        return claim

    def events(self, state):
        records = [decode_record(frame) for frame in state.drain()]
        self.assertEqual([record['event_seq'] for record in records],
                         list(range(1, len(records) + 1)))
        return records

    def complete(self, outcome='return'):
        state = self.state()
        owner = self.ready(state)
        claim = self.consume(state, owner)
        self.assertTrue(state.activity(claim, owner, state.bindings.target))
        self.assertTrue(state.activity(claim, owner, state.bindings.target))  # Actual recursive entries.
        self.assertTrue(state.root_exit(claim, outcome))
        self.assertTrue(state.provisional_end(Health()))  # Export may still be outstanding.
        state.retire_context(owner)
        return state, owner, claim

    def test_normal_and_exception_outcomes_need_final_health_and_retired_owner(self):
        for outcome in ('return', 'exception'):
            with self.subTest(outcome=outcome):
                state, _, _ = self.complete(outcome)
                self.assertTrue(state.finish(Health(export_finished=True)))
                self.assertIsNone(state.finish(Health(export_finished=True)))
                records = self.events(state)
                self.assertEqual([r['event'] for r in records],
                                 ['ready', 'consumed', 'root_exit', 'end', 'runtime_end'])
                self.assertEqual(records[2]['outcome'], outcome)
                self.assertTrue(records[3]['provisional'])
                self.assertTrue(records[-1]['complete'])
                self.assertEqual(records[-1]['health']['losses'], 0)
                self.assertEqual(state.target_entries, 2)
                self.assertIsNone(state.handle(b'anything\n'))

    def test_same_digest_wrong_thread_context_and_call_site_do_not_consume(self):
        state = self.state()
        owner = self.ready(state)
        self.assertTrue(self.arm(state)['ok'])
        observed = []

        def wrong_thread():
            observed.append(state.claim_wrapper(state.bindings.wrapper, object(), state.bindings.call_site))

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [None])
        self.assertIsNone(state.claim_wrapper(Probe(state.bindings.wrapper.digest), owner, state.bindings.call_site))
        self.assertIsNone(state.claim_wrapper(state.bindings.wrapper, owner, object()))
        self.assertIsNone(state.claim_wrapper(state.bindings.wrapper, state.context_id, state.bindings.call_site))
        self.assertEqual(state.phase, 'armed')
        self.assertIsNone(state.fault)
        claim = state.claim_wrapper(state.bindings.wrapper, owner, state.bindings.call_site)
        self.assertTrue(state.bind_root(claim, 123, 456, sampled=True))
        self.assertEqual(state.phase, 'consumed')

    def test_intended_call_site_reached_before_arm_is_not_salvaged_by_a_later_arm(self):
        state = self.state()
        owner = self.ready(state)
        self.assertIsNone(state.claim_wrapper(state.bindings.wrapper, owner, state.bindings.call_site))
        self.assertEqual(state.fault, 'invalid_phase')
        self.assertIsNone(self.arm(state))
        self.assertFalse(state.finish(Health(export_finished=True)))

    def test_wrong_arm_runtime_cookie_sequence_and_compiler_binding_fail_permanently(self):
        for changes, reason in (({'runtime_id': '1' * 32}, 'wrong_runtime'),
                                ({'context_id': '2' * 32}, 'wrong_context'),
                                ({'ready_seq': 2}, 'stale_readiness'),
                                ({'wrapper_probe_id': '3' * 64}, 'wrong_binding'),
                                ({'target_probe_id': '4' * 64}, 'wrong_binding'),
                                ({'build_id': '5' * 64}, 'wrong_binding'),
                                ({'selection_id': '6' * 64}, 'wrong_binding')):
            with self.subTest(reason=reason, changes=changes):
                state = self.state()
                self.ready(state)
                reply = self.arm(state, **changes)
                self.assertFalse(reply['ok'])
                self.assertEqual(reply['error'], reason)
                self.assertIsNone(self.arm(state, request_number=2))
                self.assertFalse(state.finish(Health(export_finished=True)))
                self.assertFalse(self.events(state)[-1]['complete'])

    def test_malformed_or_duplicate_request_identity_never_invents_an_echo(self):
        cases = [b'{"version":1,"kind":"request","op":"execute","request_id":"' + b'1' * 32 + b'"}\n',
                 b'{"version":1,"version":1}\n', b'private contents\n', b'x' * 16385]
        for frame in cases:
            with self.subTest(frame=frame[:32]):
                state = self.state()
                self.assertIsNone(state.handle(frame))
                records = self.events(state)
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['event'], 'invalidated')
                self.assertEqual(records[0]['runtime_id'], state.runtime_id)
                self.assertNotIn('request_id', records[0])
                self.assertNotIn('op', records[0])
                self.assertNotIn(b'private contents', json.dumps(records).encode())
        state = self.state()
        query = {'version': 1, 'kind': 'request', 'op': 'capabilities',
                 'request_id': '1' * 32, 'profile': 'python-sync-v1'}
        self.assertIsNotNone(state.handle(encode_record(query)))
        self.assertIsNone(state.handle(encode_record(query)))
        self.assertEqual(state.fault, 'duplicate_request')

    def test_request_history_and_event_buffer_have_explicit_bounds(self):
        state = self.state()
        for number in range(1, MAX_REQUESTS + 1):
            query = {'version': 1, 'kind': 'request', 'op': 'capabilities',
                     'request_id': f'{number:032x}', 'profile': 'python-sync-v1'}
            self.assertTrue(decode_record(state.handle(encode_record(query)))['available'])
        self.assertIsNone(state.handle(encode_record(dict(query, request_id='f' * 32))))
        self.assertEqual(len(state.request_ids), MAX_REQUESTS)
        self.assertEqual(state.request_count, MAX_REQUESTS)
        self.assertEqual(state.fault, 'request_capacity')
        for _ in range(100):
            self.assertIsNone(state.handle(b'ignored after refusal\n'))
        self.assertEqual(len(state.request_ids), MAX_REQUESTS)
        with patch('quux_otelc_python.witness_state.MAX_EVENTS', 4):
            state = self.state()
            owner = self.ready(state)
            claim = self.consume(state, owner)
            state.activity(claim, owner, state.bindings.target)
            self.assertFalse(state.root_exit(claim, 'return'))
            self.assertFalse(state.finish(Health(export_finished=True)))
            records = self.events(state)
            self.assertEqual(len(records), 4)
            self.assertEqual(records[-2]['reason'], 'event_capacity')
            self.assertFalse(records[-1]['complete'])

    def test_duplicate_arm_consume_and_root_exit_cannot_restore_success(self):
        for stage in ('arm', 'bind', 'exit'):
            with self.subTest(stage=stage):
                state = self.state()
                owner = self.ready(state)
                claim = self.consume(state, owner)
                if stage == 'arm':
                    self.assertEqual(self.arm(state, request_number=2)['error'], 'scope_reused')
                elif stage == 'bind':
                    self.assertFalse(state.bind_root(claim, 123, 456, sampled=True))
                else:
                    self.assertTrue(state.root_exit(claim, 'return'))
                    self.assertFalse(state.root_exit(claim, 'return'))
                self.assertFalse(state.finish(Health(export_finished=True)))

    def test_missing_target_or_provisional_end_and_live_owner_are_incomplete(self):
        for omission in ('target', 'end', 'owner'):
            with self.subTest(omission=omission):
                state = self.state()
                owner = self.ready(state)
                claim = self.consume(state, owner)
                if omission != 'target':
                    state.activity(claim, owner, state.bindings.target)
                state.root_exit(claim, 'return')
                if omission != 'end':
                    state.provisional_end(Health())
                if omission != 'owner':
                    state.retire_context(owner)
                self.assertFalse(state.finish(Health(export_finished=True)))

    def test_cancel_before_or_after_consume_permanently_retires_the_scope(self):
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                state = self.state()
                owner = self.ready(state)
                if consumed:
                    self.consume(state, owner)
                else:
                    self.arm(state)
                cancel = {'version': 1, 'kind': 'request', 'op': 'cancel', 'request_id': '2' * 32,
                          'runtime_id': state.runtime_id, 'scope_id': state.scope_id}
                reply = decode_record(state.handle(encode_record(cancel)))
                self.assertTrue(reply['ok'])
                self.assertEqual(reply['consumed'], consumed)
                self.assertIsNone(state.claim_wrapper(state.bindings.wrapper, owner, state.bindings.call_site))
                self.assertFalse(state.finish(Health(export_finished=True)))

    def test_late_activity_invalidates_provisional_and_even_an_already_emitted_terminal(self):
        state, owner, claim = self.complete()
        self.assertFalse(state.activity(claim, owner, state.bindings.target))
        self.assertFalse(state.finish(Health(export_finished=True)))
        records = self.events(state)
        self.assertEqual(records[-2]['reason'], 'late_activity')
        self.assertFalse(records[-1]['complete'])
        state, owner, claim = self.complete()
        self.assertTrue(state.finish(Health(export_finished=True)))
        self.assertFalse(state.activity(claim, owner, state.bindings.target))
        records = self.events(state)
        self.assertEqual(records[-2]['event'], 'runtime_end')
        self.assertEqual(records[-1]['event'], 'invalidated')
        self.assertEqual(records[-1]['reason'], 'late_activity')

    def test_dirty_provisional_health_cannot_be_erased_by_a_clean_terminal_snapshot(self):
        for health in (Health(inflight=1), Health(pending=1), Health(losses=1)):
            with self.subTest(health=health):
                state = self.state()
                owner = self.ready(state)
                claim = self.consume(state, owner)
                state.activity(claim, owner, state.bindings.target)
                state.root_exit(claim, 'return')
                state.provisional_end(health)
                state.retire_context(owner)
                self.assertFalse(state.finish(Health(export_finished=True)))

    def test_unavailable_profile_and_retired_context_never_arm(self):
        state = self.state(available=False)
        self.assertFalse(state.rendezvous(state.bindings.rendezvous, object()))
        query = {'version': 1, 'kind': 'request', 'op': 'capabilities', 'request_id': '1' * 32,
                 'profile': 'python-sync-v1'}
        self.assertFalse(decode_record(state.handle(encode_record(query)))['available'])
        other = dict(query, request_id='2' * 32, profile='node-sequence-v1')
        self.assertEqual(decode_record(state.handle(encode_record(other)))['reason'], 'unsupported_profile')
        state = self.state()
        owner = self.ready(state)
        state.retire_context(owner)
        self.assertIsNone(self.arm(state))
        self.assertFalse(state.finish(Health(export_finished=True)))

    def test_invalid_sdk_ids_sampling_and_health_are_never_reported_as_complete(self):
        for trace_id, span_id, sampled in ((0, 1, True), (1, 0, True), (True, 1, True),
                                           (1 << 128, 1, True), (1, 1 << 64, True),
                                           (1, 1, False), (1, 1, 1)):
            with self.subTest(trace_id=trace_id, span_id=span_id, sampled=sampled):
                state = self.state()
                owner = self.ready(state)
                self.arm(state)
                claim = state.claim_wrapper(state.bindings.wrapper, owner, state.bindings.call_site)
                self.assertFalse(state.bind_root(claim, trace_id, span_id, sampled=sampled))
                self.assertFalse(state.finish(Health(export_finished=True)))
        state = self.state()
        self.assertFalse(state.finish(object()))
        for values in ({'inflight': True}, {'pending': -1}, {'losses': MAX_INTEGER + 1},
                       {'export_finished': 1}):
            with self.subTest(values=values), self.assertRaises(ProtocolError):
                Health(**values)

    def test_nonzero_random_id_and_binding_validation(self):
        with patch('quux_otelc_python.witness_state.secrets.token_hex', side_effect=['0' * 32, '1' * 32]):
            self.assertEqual(fresh_identity(), '1' * 32)
        with self.assertRaises(ProtocolError):
            Probe('not a digest')
        bindings = self.state().bindings
        call_site = object()
        with self.assertRaises(ProtocolError):
            Bindings(bindings.build_id, bindings.selection_id, bindings.rendezvous,
                     bindings.rendezvous, bindings.target, call_site)
        with self.assertRaises(ProtocolError):
            WitnessState(bindings, available=1)

    def test_foreign_handles_do_not_affect_the_live_scope_and_drains_keep_sequence(self):
        state = self.state()
        owner = self.ready(state)
        first = [decode_record(frame) for frame in state.drain()]
        self.assertEqual(first[0]['event_seq'], 1)
        claim = self.consume(state, owner)
        self.assertFalse(state.bind_root(object(), 1, 2, sampled=True))
        self.assertFalse(state.activity(object(), owner, state.bindings.target))
        self.assertFalse(state.activity(claim, object(), state.bindings.target))
        self.assertFalse(state.root_exit(object(), 'return'))
        state.retire_context(object())
        self.assertIsNone(state.fault)
        self.assertTrue(state.owner_live)
        state.activity(claim, owner, state.bindings.target)
        state.root_exit(claim, 'return')
        state.provisional_end(Health())
        state.retire_context(owner)
        self.assertTrue(state.finish(Health(export_finished=True)))
        later = [decode_record(frame) for frame in state.drain()]
        self.assertEqual([record['event_seq'] for record in first + later], [1, 2, 3, 4, 5])
        self.assertFalse(state.drain())
