"""Handle evidence requires an explicit version and the actual route proof."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from otelc import prepared_java, witness
from otelc.config import Policy
from otelc.__main__ import main
import test_witness_protocol as direct


def reply() -> dict:
    result = direct.reply() | {'schema_version': 3, 'profile': witness.HANDLE_PROFILE,
                              'route_binding_sha256': '4' * 64}
    result['scope']['route_verified'] = True
    return result


class HandleWitnessProtocolTests(unittest.TestCase):
    def test_explicit_closed_version_and_verified_route_are_required(self) -> None:
        valid = reply()
        self.assertIs(witness.validate_reply(valid, 'b' * 32, profile=witness.HANDLE_PROFILE), valid)
        with self.assertRaises(ValueError):
            witness.validate_reply(valid, 'b' * 32)
        with self.assertRaises(ValueError):
            witness.validate_reply(direct.reply(), 'b' * 32, profile=witness.HANDLE_PROFILE)
        for key, value in (('schema_version', 2), ('schema_version', True), ('profile', witness.PROFILE),
                           ('route_binding_sha256', None), ('route_binding_sha256', 'bad')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                witness.validate_reply(valid | {key: value}, 'b' * 32, profile=witness.HANDLE_PROFILE)
        for value in (False, None, 1, 'true'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                witness.validate_reply(valid | {'scope': valid['scope'] | {'route_verified': value}},
                                       'b' * 32, profile=witness.HANDLE_PROFILE)
        for value in (valid | {'extra': True}, {k: v for k, v in valid.items() if k != 'route_binding_sha256'}):
            with self.assertRaises(ValueError):
                witness.validate_reply(value, 'b' * 32, profile=witness.HANDLE_PROFILE)
        unverified = valid | {'scope': valid['scope'] | {'producer_complete': False, 'route_verified': False}}
        self.assertFalse(witness.validate_reply(unverified, 'b' * 32, profile=witness.HANDLE_PROFILE)['scope']['route_verified'])

    def test_final_contexts_keep_the_same_closed_route_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'witness-health.json'
            valid = reply()
            document = {'schema_version': 3, 'kind': 'otelc.java-witness-final', 'phase': 'final', 'pid': 42,
                        'producer': valid | {'request_id': None, 'scope': None},
                        'queue_evidence': 'unavailable', 'scopes': [valid['scope']]}
            path.write_text(json.dumps(document)); path.chmod(0o600)
            self.assertEqual(witness.read_final(path, profile=witness.HANDLE_PROFILE), document)
            with self.assertRaises(ValueError):
                witness.read_final(path)
            changed = copy.deepcopy(document)
            changed['scopes'][0]['route_verified'] = False
            path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                witness.read_final(path, profile=witness.HANDLE_PROFILE)

    def test_bound_route_cannot_change_its_field_site_descriptor_or_digest(self) -> None:
        rows = {}
        for role in witness.ROLES:
            row = {'owner': 'Harness', 'name': role, 'descriptor': '()V',
                   'class_sha256': 'a' * 64, 'method_sha256': 'b' * 64}
            row['probe_id'] = hashlib.sha256('\n'.join([witness.HANDLE_PROFILE, role,
                row['owner'], row['name'], row['descriptor'], row['class_sha256'], row['method_sha256']]).encode()).hexdigest()
            rows[role] = row
        route = {'kind': 'direct_method_handle', 'field_owner': 'Harness', 'field_name': 'HANDLE',
                 'field_descriptor': 'Ljava/lang/invoke/MethodHandle;', 'site_instruction': 0,
                 'invoke_owner': 'java/lang/invoke/MethodHandle', 'invoke_name': 'invokeExact',
                 'invoke_descriptor': '()V', 'reference_kind': 6}
        values = [witness.HANDLE_PROFILE, *(str(route[key]) for key in ('kind', 'field_owner', 'field_name',
            'field_descriptor', 'site_instruction', 'invoke_owner', 'invoke_name', 'invoke_descriptor', 'reference_kind')),
            rows['wrapper']['probe_id'], rows['target']['probe_id']]
        route['sha256'] = hashlib.sha256('\n'.join(values).encode()).hexdigest()
        application = SimpleNamespace(bindings={'Harness': {'class_sha256': 'a' * 64}})
        document = {'schema_version': 3, 'kind': 'otelc.java-witness-bindings',
                    'profile': witness.HANDLE_PROFILE, 'bindings': rows, 'route': route}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'bindings.json'
            path.write_text(json.dumps(document))
            self.assertEqual(witness.load_bindings(path, application, profile=witness.HANDLE_PROFILE), document)
            with self.assertRaises(ValueError):
                witness.load_bindings(path, application)
            for key, value in (('kind', 'reflection'), ('field_owner', 'Other'), ('field_name', ''),
                               ('field_name', 'OTHER'), ('site_instruction', True), ('site_instruction', 1),
                               ('invoke_owner', 'Other'), ('invoke_name', 'invoke'), ('invoke_descriptor', '(I)V'),
                               ('reference_kind', True), ('reference_kind', 7), ('sha256', '0' * 64)):
                changed = document | {'route': route | {key: value}}
                path.write_text(json.dumps(changed))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    witness.load_bindings(path, application, profile=witness.HANDLE_PROFILE)

    def test_unsupported_or_unbound_profiles_cannot_fall_back_to_normal_launch(self) -> None:
        with self.assertRaises(ValueError):
            witness.profile_version('future')
        with self.assertRaises(ValueError):
            prepared_java.prepare(SimpleNamespace(), Policy.parse({'schema_version': 3}),
                                  Path('unused'), witness_profile=witness.HANDLE_PROFILE)

    def test_cli_requires_an_explicit_profile_and_matching_bindings_option(self) -> None:
        arguments = ['quux-otelc', 'java', '--manifest', 'app.json', '--runtime', '/tmp/runtime',
                     '--witness-profile', witness.HANDLE_PROFILE]
        with mock.patch('sys.argv', arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main()
        with mock.patch('sys.argv', arguments + ['--witness-bindings', 'bindings.json']), \
                mock.patch('otelc.prepared_java.run', return_value=0) as run:
            self.assertEqual(main(), 0)
            self.assertEqual(run.call_args.kwargs['witness_profile'], witness.HANDLE_PROFILE)
            self.assertEqual(run.call_args.kwargs['witness_bindings'], Path('bindings.json'))
