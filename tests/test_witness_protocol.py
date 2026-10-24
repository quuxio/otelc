"""Reject contradictory or malformed producer evidence before it can qualify an action."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from otelc import witness
from otelc.config import Policy


def reply() -> dict:
    """A complete observed two-role scope, independent of any requested application value."""
    value = {"schema_version": 2, "profile": witness.PROFILE, "runtime_id": "a" * 32, "pid": 42,
             "request_id": "b" * 32, "error": None, "rejected_probes": "0",
             "max_scopes": 64, "max_spans_per_scope": 1024}
    value.update({key: "c" * 64 for key in ("build_sha256", "selection_sha256", "policy_sha256",
                                           "tooling_sha256", "wrapper_probe_id", "envelope_probe_id",
                                           "rendezvous_probe_id", "target_probe_id")})
    value["scope"] = {"cookie": "d" * 32, "sequence": "1", "thread_id": "41", "scope_id": "e" * 32,
        "case_id": "f" * 64, "attempt_id": "0" * 64, "phase": "complete", "failure": None,
        "trace_id": "1" * 32, "root_span_id": "2" * 16, "target_span_id": "3" * 16,
        "root_exited": True, "envelope_exited": True, "target_exited": True,
        "target_entries": "1", "export_failures": "0", "flush_failures": "0", "checkpoint": True,
        "producer_complete": True, "receiver_confirmation_required": True,
        "started_ids": ["2" * 16, "3" * 16], "ended_ids": ["2" * 16, "3" * 16],
        "exported_ids": ["2" * 16, "3" * 16], "pending_exports": "0", "queue_dropped": None}
    return value


class WitnessProtocolTests(unittest.TestCase):
    def test_v2_requires_actual_pid_and_positive_canonical_logical_thread(self) -> None:
        valid = reply()
        for pid in (True, 0, -1, "42"):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                witness.validate_reply(valid | {"pid": pid}, "b" * 32)
        for thread in (True, None, "0", "01", "-1", "1.0", "١", str(2**63)):
            with self.subTest(thread=thread), self.assertRaises(ValueError):
                witness.validate_reply(valid | {"scope": valid["scope"] | {"thread_id": thread}}, "b" * 32)
        for version, profile in ((1, "java-sync-action-v1"), (1, witness.PROFILE), (2, "java-sync-action-v1")):
            with self.subTest(version=version, profile=profile), self.assertRaises(ValueError):
                witness.validate_reply(valid | {"schema_version": version, "profile": profile}, "b" * 32)

    def test_fifo_bindings_are_refused_without_waiting_for_a_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bindings.json'
            os.mkfifo(path, 0o600)
            code = ('from pathlib import Path\nfrom types import SimpleNamespace\n'
                    'from otelc import witness\n'
                    'try: witness.load_bindings(Path(' + repr(str(path)) + '), SimpleNamespace(bindings={}))\n'
                    'except ValueError as error: print(type(error).__name__, str(error))\n'
                    'else: raise AssertionError("FIFO bindings were accepted")\n')
            result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'not a regular file', result.stdout)
            self.assertEqual(result.stderr, b'')

    def test_only_explicit_full_tracing_can_request_the_profile(self) -> None:
        application = SimpleNamespace()
        unused = Path("unused")
        for tracing in ({"enabled": False, "sample_ratio": 1.0},
                        {"enabled": True, "sample_ratio": 0.999999999999}):
            policy = Policy.parse({"schema_version": 3, "languages": ["java"], "traces": tracing})
            with self.subTest(tracing=tracing), self.assertRaisesRegex(ValueError, "sample_ratio=1"):
                witness.configure(application, policy, unused, unused, {}, [])
        self.assertFalse(witness.decimal("1\u0661"))

    def test_private_final_reports_retain_all_contexts_and_refuse_forged_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "witness-health.json"
            header = reply() | {"request_id": None, "scope": None}
            scope = reply()["scope"]
            document = {"schema_version": 2, "kind": "otelc.java-witness-final", "phase": "final", "pid": 42,
                        "producer": header, "queue_evidence": "unavailable", "scopes": [scope]}
            path.write_text(json.dumps(document))
            path.chmod(0o600)
            self.assertEqual(witness.read_final(path), document)
            for mutation in (document | {"unexpected": True}, document | {"producer": header | {"pid": 43}}, document | {"pid": True},
                             document | {"phase": "running"}, document | {"scopes": [None]},
                             document | {"scopes": [scope, scope]}, document | {"scopes": [scope] * 65},
                             document | {"producer": reply()},
                             document | {"scopes": [scope | {"queue_dropped": "0"}]}):
                path.write_text(json.dumps(mutation))
                with self.assertRaises(ValueError):
                    witness.read_final(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                witness.read_final(path)
            path.unlink()
            path.symlink_to(root / "missing")
            with self.assertRaises(ValueError):
                witness.read_final(path)
            with self.assertRaises(ValueError):
                witness.read_final(root / "wrong.json")

    def test_closed_header_and_scope_shapes_and_exact_identity_types(self) -> None:
        valid = reply()
        self.assertIs(witness.validate_reply(valid, "b" * 32), valid)
        self.assertIsNone(witness.validate_reply(valid | {"scope": None}, "b" * 32)["scope"])
        outer = [("schema_version", True), ("profile", "other"), ("runtime_id", "invalid"),
                 ("request_id", "c" * 32), ("error", []), ("rejected_probes", 0),
                 ("max_scopes", True), ("max_spans_per_scope", 1), ("build_sha256", "0")]
        for key, value in outer:
            with self.subTest(key=key), self.assertRaises(ValueError):
                witness.validate_reply(valid | {key: value}, "b" * 32)
        for value in (None, [], valid | {"unexpected": 1}, {k: v for k, v in valid.items() if k != "error"}):
            with self.assertRaises(ValueError):
                witness.validate_reply(value, "b" * 32)
        bad_scopes = [[], {}, valid["scope"] | {"extra": 1}, valid["scope"] | {"phase": []},
                      valid["scope"] | {"phase": "unknown"}, valid["scope"] | {"cookie": "no"},
                      valid["scope"] | {"sequence": "0"}, valid["scope"] | {"failure": []},
                      valid["scope"] | {"root_exited": 1}, valid["scope"] | {"receiver_confirmation_required": False}]
        for scope in bad_scopes:
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                witness.validate_reply(valid | {"scope": scope}, "b" * 32)

    def test_completion_requires_all_actual_span_ids_and_matching_exit_barrier(self) -> None:
        valid = reply()
        mutations = [("exported_ids", []), ("ended_ids", []), ("started_ids", []),
                     ("pending_exports", "1"), ("checkpoint", False), ("root_exited", False),
                     ("envelope_exited", False), ("target_exited", False), ("failure", "cancelled"),
                     ("target_entries", "0"), ("phase", "exited"), ("root_span_id", "4" * 16), ("scope_id", None),
                     ("exported_ids", ["4" * 16]), ("started_ids", ["2" * 16] * 2),
                     ("started_ids", [[]]), ("started_ids", ["2" * 16] * 1025)]
        for key, value in mutations:
            with self.subTest(key=key), self.assertRaises(ValueError):
                witness.validate_reply(valid | {"scope": valid["scope"] | {key: value}}, "b" * 32)
        pending = copy.deepcopy(valid)
        pending["scope"].update(producer_complete=False, checkpoint=False, exported_ids=[], pending_exports="2")
        pending["error"] = "incomplete"
        self.assertFalse(witness.validate_reply(pending, "b" * 32)["scope"]["producer_complete"])
        for key in ("ended_ids", "exported_ids"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                witness.validate_reply(pending | {"scope": pending["scope"] | {key: ["4" * 16]}}, "b" * 32)

    def test_binding_documents_reject_unknown_missing_duplicate_and_forged_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            application = SimpleNamespace(bindings={"Example": {"class_sha256": "a" * 64}})
            bindings = {}
            for role in witness.ROLES:
                binding = {"owner": "Example", "name": role, "descriptor": "()V",
                           "class_sha256": "a" * 64, "method_sha256": "b" * 64}
                text = "\n".join([witness.PROFILE, role, "Example", role, "()V", "a" * 64, "b" * 64])
                binding["probe_id"] = hashlib.sha256(text.encode()).hexdigest()
                bindings[role] = binding
            document = {"schema_version": 2, "kind": "otelc.java-witness-bindings", "profile": witness.PROFILE,
                        "bindings": bindings}
            path.write_text(json.dumps(document))
            self.assertEqual(witness.load_bindings(path, application), document)
            for mutation in (document | {"extra": 1}, document | {"schema_version": True}, document | {"bindings": {}},
                             document | {"schema_version": 1}, document | {"profile": "unknown"}, document | {"kind": "other"}):
                path.write_text(json.dumps(mutation))
                with self.assertRaises(ValueError):
                    witness.load_bindings(path, application)
            for key, value in (("extra", "value"), ("owner", "Missing"), ("class_sha256", "c" * 64),
                               ("method_sha256", "invalid"), ("probe_id", "0" * 64), ("name", "")):
                mutation = copy.deepcopy(document)
                mutation["bindings"]["target"][key] = value
                path.write_text(json.dumps(mutation))
                with self.assertRaises(ValueError):
                    witness.load_bindings(path, application)
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):
                witness.load_bindings(path, application)
            path.write_text("x" * 32769)
            with self.assertRaises(ValueError):
                witness.load_bindings(path, application)
