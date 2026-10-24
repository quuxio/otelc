"""Frozen offline bindings, actual role advice, live ownership and independently decoded OTLP."""

from __future__ import annotations

import json
import re
import select
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from otelc import control, java, prepared_java, witness
from otelc.config import Policy
from test_java_integration import Capture, wait_for_count
from test_prepared_java import binding

SOURCE = r'''
import java.io.*;
public final class Harness {
  static final BufferedReader INPUT = new BufferedReader(new InputStreamReader(System.in));
  static void rendezvous() throws IOException {
    System.out.println("release:" + Thread.currentThread().threadId());
    if (!"go".equals(INPUT.readLine())) throw new IOException("missing release");
  }
  static int wrapper(int seed) { return Work.target(seed); }
  static int envelope(int seed) throws IOException { rendezvous(); return wrapper(seed); }
  public static void main(String[] args) throws IOException {
    System.out.println("ready");
    for (String line; (line = INPUT.readLine()) != null && !line.equals("quit");) {
      try {
        int value = envelope(Integer.parseInt(line));
        System.out.println("{\"value\":" + value + ",\"getters\":" + Work.getters + "}");
      } catch (Work.Problem error) {
        System.out.println("{\"error\":true,\"same\":" + (error == Work.FAILURE) + ",\"getters\":" + Work.getters + "}");
      }
    }
  }
}
final class Work {
  static int getters;
  static final Problem FAILURE = new Problem();
  static int target(int seed) { if (seed < 0) throw FAILURE; return seed + 1; }
  static final class Problem extends RuntimeException {
    @Override public String getMessage() { getters++; return "changed"; }
    @Override public String toString() { getters++; return "changed"; }
    @Override public synchronized Throwable getCause() { getters++; return this; }
  }
}
'''

RECEIVER_SOURCE = SOURCE.replace("static int wrapper(int seed) { return Work.target(seed); }",
    "static int wrapper(Work receiver, int seed) { return receiver.target(seed); }") \
    .replace("static int envelope(int seed) throws IOException { rendezvous(); return wrapper(seed); }",
             "static int envelope(Work receiver, int seed) throws IOException { rendezvous(); return wrapper(receiver, seed); }") \
    .replace("int value = envelope(Integer.parseInt(line));",
             "Work receiver = new Work(); int value = envelope(receiver, Integer.parseInt(line));") \
    .replace("final class Work {", "class Work {\n  int offset = 1;") \
    .replace("static int target(int seed) { if (seed < 0) throw FAILURE; return seed + 1; }",
             "int target(int seed) { if (seed < 0) throw FAILURE; return seed + offset; }")

OVERRIDE_SOURCE = RECEIVER_SOURCE.replace("Work receiver = new Work();", "Work receiver = new OverrideWork();") + r'''
final class OverrideWork extends Work {
  @Override int target(int seed) { if (seed < 0) throw FAILURE; return seed + 2; }
}
'''


def read_line(process: subprocess.Popen[str]) -> str:
    """Read one ordinary fixture message with an actual process deadline."""
    if process.stdout is None or not select.select([process.stdout], [], [], 15)[0]:
        raise AssertionError("fixture did not reply")
    return process.stdout.readline().strip()


def read_release(process: subprocess.Popen[str]) -> str:
    """Obtain the expected Java thread from the SDK-free fixture, not the producer."""
    frame = read_line(process)
    match = re.fullmatch(r"release:([1-9][0-9]*)", frame)
    if match is None:
        raise AssertionError(f"missing independent harness readiness: {frame!r}")
    return match.group(1)


def send(process: subprocess.Popen[str], value: str) -> None:
    """Use the same fixture channel in both lanes; no telemetry API enters the app."""
    assert process.stdin is not None
    process.stdin.write(value + "\n")
    process.stdin.flush()


class WitnessIntegrationTests(unittest.TestCase):
    def test_returned_future_cannot_complete_a_scope_before_the_observed_operation_ends(self) -> None:
        source_text = r'''
import java.io.*;
import java.util.concurrent.CompletableFuture;
public final class Harness {
  static final BufferedReader INPUT = new BufferedReader(new InputStreamReader(System.in));
  static void rendezvous() throws IOException { System.out.println("release:" + Thread.currentThread().threadId()); INPUT.readLine(); }
  static CompletableFuture<Integer> wrapper(int seed) { return Work.target(seed); }
  static CompletableFuture<Integer> envelope(int seed) throws IOException { rendezvous(); return wrapper(seed); }
  public static void main(String[] args) throws Exception {
    System.out.println("ready"); INPUT.readLine();
    CompletableFuture<Integer> result = envelope(7);
    System.out.println((result == Work.pending) + ":" + result.isDone());
    INPUT.readLine(); Work.pending.complete(42); System.out.println(result.join());
    INPUT.readLine();
  }
}
final class Work {
  static final CompletableFuture<Integer> pending = new CompletableFuture<>();
  static CompletableFuture<Integer> target(int seed) { return pending; }
}
'''
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix="otelc-fut-", dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "Harness.java"
            source.write_text(source_text)
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Harness", "classpath": ["classes"],
                "bindings": {owner: binding(source, classes / f"{owner}.class", "src/Harness.java")
                             for owner in ("Harness", "Work")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            declarations = {role: {"owner": "Work" if role == "target" else "Harness", "name": role,
                "descriptor": "()V" if role == "rendezvous" else "(I)Ljava/util/concurrent/CompletableFuture;"}
                for role in witness.ROLES}
            frozen = root / "bindings.json"
            witness.compile_bindings(application, declarations, frozen)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]}, "functions": {"include": ["Work.target(*)"]},
                "metrics": {"collection": False, "emission": False},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain,
                    diagnostic_profile=True, **({} if plain else {"witness_bindings": frozen}))
                with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertEqual(read_line(process), "ready")
                        send(process, "start")
                        expected_thread = read_release(process)
                        socket = runtime / "witness.sock"
                        if not plain:
                            control.wait_ready(runtime / "control.sock", process.pid)
                            status = witness.request(socket, "status")
                            self.assertEqual(status["pid"], process.pid)
                            self.assertEqual(status["scope"]["thread_id"], expected_thread)
                            arm = witness.request(socket, "arm", status["runtime_id"], status["scope"]["cookie"],
                                status["scope"]["sequence"], "a" * 64, "b" * 64, status["build_sha256"],
                                status["selection_sha256"], status["tooling_sha256"], status["wrapper_probe_id"],
                                status["policy_sha256"], expected_thread)
                            self.assertIsNone(arm["error"], arm)
                        send(process, "go")
                        self.assertEqual(read_line(process), "true:false")
                        if not plain:
                            pending = witness.request(socket, "end", status["runtime_id"], arm["scope"]["scope_id"])
                            self.assertEqual(pending["error"], "incomplete", pending)
                            self.assertFalse(pending["scope"]["producer_complete"])
                            self.assertEqual(len(pending["scope"]["started_ids"]), 3)
                            self.assertEqual(len(pending["scope"]["ended_ids"]), 2)
                        send(process, "complete")
                        self.assertEqual(read_line(process), "42")
                        if not plain:
                            deadline = time.monotonic() + 10
                            while True:
                                before_end = witness.request(socket, "status", status["runtime_id"], arm["scope"]["scope_id"])
                                if len(before_end["scope"]["exported_ids"]) == 3 or time.monotonic() >= deadline:
                                    break
                                time.sleep(.02)
                            self.assertEqual(len(before_end["scope"]["exported_ids"]), 3, before_end)
                            self.assertFalse(before_end["scope"]["producer_complete"], before_end)
                            self.assertFalse(before_end["scope"]["checkpoint"], before_end)
                            settled = witness.request(socket, "end", status["runtime_id"], arm["scope"]["scope_id"])
                            self.assertTrue(settled["scope"]["producer_complete"], settled)
                            self.assertEqual(settled["scope"]["started_ids"], settled["scope"]["exported_ids"])
                        stdout, stderr = process.communicate("quit\n", timeout=15)
                        self.assertEqual((process.returncode, stdout, stderr), (0, "", ""))
                        launch.verify_inputs()
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual({span.span_id.hex() for span in spans}, set(settled["scope"]["exported_ids"]))

    def test_all_live_metric_states_keep_independent_action_traces_in_one_jvm(self) -> None:
        self.exercise(SOURCE, live_metrics=True)

    def test_private_witness_helper_and_class_initialiser_calls_cannot_replace_the_bound_action(self) -> None:
        source = SOURCE.replace("static int getters;", "static int getters; static final int INITIAL = target(40);")
        source = source.replace("return seed + 1;", "return seed + io.quux.otelc.witness.probe.Observer.applicationValue();")
        self.exercise(source, helper_collision=True, extra_unscoped=1)

    def test_actual_bound_calls_sequential_scopes_errors_and_decoded_export(self) -> None:
        self.exercise(SOURCE)

    def test_receiver_calls_preserve_state_and_exception_identity(self) -> None:
        self.exercise(RECEIVER_SOURCE, receiver=True)

    def test_virtual_dispatch_cannot_substitute_a_different_target_implementation(self) -> None:
        self.exercise(OVERRIDE_SOURCE, receiver=True, substituted=True)

    def test_runtime_archive_members_drive_bindings_and_actual_action_evidence(self) -> None:
        for archive in ('ordinary', 'active', 'versioned-only'):
            with self.subTest(archive=archive):
                self.exercise(SOURCE, archive=archive)

    def exercise(self, source_text: str, *, receiver: bool = False, substituted: bool = False,
                 helper_collision: bool = False, extra_unscoped: int = 0,
                 live_metrics: bool = False, archive: str | None = None,
                 profile: str = witness.PROFILE, expected_refusal: str = "target_not_entered",
                 substituted_value: int = 9, recursive_entries: int = 1,
                 argument_descriptor: str = "I") -> None:
        def exchange(path, operation, *arguments):
            return witness.request(path, operation, *arguments, profile=profile)

        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix="otelc-wit-", dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "Harness.java"
            source.write_text(source_text)
            sources = [source]
            if helper_collision:
                helper = root / "Observer.java"
                helper.write_text("package io.quux.otelc.witness.probe; public class Observer {"
                                  " public static int applicationValue() { return 1; }}")
                sources.append(helper)
            subprocess.run([java.executable("javac"), "-d", str(classes), *map(str, sources)], check=True)
            classpath = "classes"
            if archive is not None:
                classpath = "application.jar"
                base = root / 'base'
                base.mkdir()
                base_source = base / 'Harness.java'
                base_source.write_text(source_text.replace('return seed + 1;', 'return seed + 99;')
                                       .replace('Thread.currentThread().threadId()', 'Thread.currentThread().getId()'))
                subprocess.run([java.executable('javac'), '--release', '9', '-d', str(base), str(base_source)], check=True)
                version = subprocess.check_output([java.executable('java'), '-version'], stderr=subprocess.STDOUT, text=True)
                feature = int(re.search(r'version "([0-9]+)', version)[1])
                with zipfile.ZipFile(root / classpath, 'w') as bundle:
                    bundle.writestr('META-INF/MANIFEST.MF', 'Manifest-Version: 1.0\r\n'
                                     + ('Multi-Release: true\r\n' if archive != 'ordinary' else '') + '\r\n')
                    if archive != 'versioned-only':
                        selected = classes if archive == 'ordinary' else base
                        for path in selected.rglob('*.class'):
                            bundle.write(path, path.relative_to(selected).as_posix())
                    if archive != 'ordinary':
                        for path in classes.rglob('*.class'):
                            bundle.write(path, f'META-INF/versions/{feature}/' + path.relative_to(classes).as_posix())
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Harness", "classpath": [classpath],
                "bindings": {owner: binding(source, classes / f"{owner}.class", "src/Harness.java")
                             for owner in ("Harness", "Work")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            declarations = {role: {"owner": "Work" if role == "target" else "Harness", "name": role,
                                  "descriptor": "()V" if role == "rendezvous" else
                                      f"(LWork;{argument_descriptor})I" if receiver and role in ("envelope", "wrapper")
                                      else f"({argument_descriptor})I"}
                            for role in witness.ROLES}
            frozen = root / "bindings.json"
            compiler_result = witness.compile_bindings(application, declarations, frozen, profile=profile)
            self.assertEqual(set(compiler_result["bindings"]), set(witness.ROLES))
            route_words = (compiler_result["route"]["sha256"],) if profile == witness.HANDLE_PROFILE else ()
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]}, "functions": {"include": ["Work.target(*)"]},
                "metrics": {"collection": False, "emission": False},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}", "timeout_ms": 10000}})
            outputs = []
            scopes = []
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                options = {} if plain else {"witness_bindings": frozen, "witness_profile": profile}
                launch = prepared_java.prepare(application, policy, runtime, plain=plain,
                                               diagnostic_profile=True, **options)
                self.assertEqual(len(launch.identity), 16)
                with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertEqual(read_line(process), "ready")
                        socket = runtime / "witness.sock"
                        if not plain:
                            control.wait_ready(runtime / "control.sock", process.pid)
                            deadline = time.monotonic() + 5
                            while not socket.exists() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertTrue(socket.exists(), (runtime / "agent.log").read_text())
                        observed = []
                        for index, seed in enumerate((7,) if substituted else (7, -1, 2, 3, 4)):
                            if not plain and live_metrics:
                                collection, emission = ((False, False), (True, False), (False, True),
                                                        (True, True), (False, False))[index]
                                metrics_socket = runtime / "control.sock"
                                control.request(metrics_socket, "collection " + ("on" if collection else "off"))
                                state = control.request(metrics_socket, "emission " + ("on" if emission else "off"))
                                self.assertEqual((state["pid"], state["collection"], state["emission"]),
                                                 (process.pid, collection, emission))
                            send(process, str(seed))
                            expected_thread = read_release(process)
                            if not plain:
                                status = exchange(socket, "status")
                                self.assertEqual(status["pid"], process.pid)
                                self.assertEqual(status["scope"]["thread_id"], expected_thread)
                                self.assertIsNone(status["error"], (runtime / "agent.log").read_text())
                                self.assertEqual(status["scope"]["phase"], "ready", status)
                                if route_words:
                                    self.assertEqual(status["route_binding_sha256"], route_words[0])
                                    self.assertFalse(status["scope"]["route_verified"])
                                for role in witness.ROLES:
                                    self.assertEqual(status[role + "_probe_id"], compiler_result["bindings"][role]["probe_id"])
                                if index == 0 and route_words:
                                    wrong_route = exchange(socket, "arm", status["runtime_id"],
                                        status["scope"]["cookie"], status["scope"]["sequence"], "a" * 64,
                                        f"{index + 1:064x}", status["build_sha256"], status["selection_sha256"],
                                        status["tooling_sha256"], status["wrapper_probe_id"], status["policy_sha256"],
                                        expected_thread, "0" * 64)
                                    self.assertEqual(wrong_route["error"], "arm_identity_mismatch")
                                    self.assertEqual(wrong_route["scope"]["phase"], "ready")
                                    self.assertIsNone(wrong_route["scope"]["scope_id"])
                                if index == 0:
                                    wrong = exchange(socket, "arm", status["runtime_id"],
                                        status["scope"]["cookie"], status["scope"]["sequence"], "a" * 64,
                                        f"{index + 1:064x}", status["build_sha256"], status["selection_sha256"],
                                        status["tooling_sha256"], status["wrapper_probe_id"], status["policy_sha256"],
                                        str(int(expected_thread) + 1), *route_words)
                                    self.assertEqual(wrong["error"], "arm_identity_mismatch")
                                    self.assertEqual(wrong["scope"]["phase"], "ready")
                                    self.assertIsNone(wrong["scope"]["scope_id"])
                                if index == 3:
                                    stale = exchange(socket, "arm", "0" * 32,
                                        status["scope"]["cookie"], status["scope"]["sequence"], "a" * 64,
                                        f"{index + 1:064x}", status["build_sha256"], status["selection_sha256"],
                                        status["tooling_sha256"], status["wrapper_probe_id"], status["policy_sha256"], expected_thread, *route_words)
                                    self.assertEqual(stale["error"], "arm_identity_mismatch")
                                    self.assertIsNone(stale["scope"]["scope_id"])
                                arm = exchange(socket, "arm", status["runtime_id"],
                                    status["scope"]["cookie"], status["scope"]["sequence"], "a" * 64,
                                    f"{index + 1:064x}", status["build_sha256"], status["selection_sha256"],
                                    status["tooling_sha256"], status["wrapper_probe_id"], status["policy_sha256"], expected_thread, *route_words)
                                self.assertIsNone(arm["error"], arm)
                                self.assertEqual(arm["scope"]["phase"], "armed")
                                if route_words:
                                    self.assertFalse(arm["scope"]["route_verified"])
                                if index == 2:
                                    cancelled = exchange(socket, "cancel", status["runtime_id"], arm["scope"]["scope_id"])
                                    self.assertEqual(cancelled["scope"]["failure"], "cancelled")
                                if index == 3:
                                    capture.trace_request_seen.clear()
                                    capture.trace_response_gate.clear()
                                if index == 4:
                                    capture.trace_response_code = 400
                            send(process, "go")
                            observed.append(json.loads(read_line(process)))
                            if not plain:
                                end = exchange(socket, "end", status["runtime_id"], arm["scope"]["scope_id"])
                                if substituted:
                                    self.assertEqual(end["error"], "incomplete", end)
                                    self.assertEqual(end["scope"]["failure"], expected_refusal, end)
                                    self.assertIsNone(end["scope"]["target_span_id"])
                                    self.assertEqual(end["scope"]["target_entries"], "0")
                                    self.assertFalse(end["scope"]["producer_complete"])
                                    continue
                                if index == 2:
                                    self.assertEqual(end["error"], "incomplete", end)
                                    self.assertEqual(end["scope"]["failure"], "cancelled")
                                    self.assertFalse(end["scope"]["producer_complete"])
                                    continue
                                if index == 3:
                                    self.assertTrue(capture.trace_request_seen.wait(timeout=2))
                                    self.assertEqual(end["error"], "incomplete", end)
                                    self.assertFalse(end["scope"]["producer_complete"])
                                    self.assertGreater(int(end["scope"]["pending_exports"]), 0)
                                    capture.trace_response_gate.set()
                                    end = exchange(socket, "end", status["runtime_id"], arm["scope"]["scope_id"])
                                    self.assertEqual(end["scope"]["flush_failures"], "1")
                                    if live_metrics:
                                        control.request(runtime / "control.sock", "flush")
                                        self.assertEqual(wait_for_count(capture.calls, 2), 2)
                                if index == 4:
                                    self.assertEqual(end["error"], "incomplete", end)
                                    self.assertFalse(end["scope"]["producer_complete"])
                                    self.assertEqual(end["scope"]["export_failures"], "3")
                                    self.assertEqual(end["scope"]["exported_ids"], [])
                                    continue
                                self.assertIsNone(end["error"], end)
                                self.assertTrue(end["scope"]["producer_complete"], end)
                                if route_words:
                                    self.assertTrue(end["scope"]["route_verified"])
                                self.assertEqual(len(end["scope"]["started_ids"]), 2 * (recursive_entries if index == 0 else 1) + 1, end)
                                self.assertEqual(int(end["scope"]["target_entries"]), recursive_entries if index == 0 else 1)
                                self.assertEqual(end["scope"]["started_ids"], end["scope"]["ended_ids"])
                                self.assertEqual(end["scope"]["started_ids"], end["scope"]["exported_ids"])
                                scopes.append(end["scope"])
                        send(process, "quit")
                        stdout, stderr = process.communicate(timeout=15)
                        self.assertEqual((stdout, stderr, process.returncode), ("", "", 0))
                        outputs.append(observed)
                        launch.verify_inputs()
                        if not plain:
                            final = witness.read_final(runtime / "witness-health.json", profile=profile)
                            self.assertEqual(final["queue_evidence"], "derived_after_shutdown")
                            self.assertEqual(len(final["scopes"]), 1 if substituted else 5)
                            self.assertEqual(final["pid"], process.pid)
                            self.assertEqual(final["producer"]["pid"], process.pid)
                            self.assertEqual({item["thread_id"] for item in final["scopes"]}, {expected_thread})
                            self.assertEqual([item["producer_complete"] for item in final["scopes"]],
                                [False] if substituted else [True, True, False, True, False])
                            self.assertEqual([item["queue_dropped"] for item in final["scopes"]], ["0"] * (1 if substituted else 5))
                    finally:
                        capture.trace_response_gate.set()
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            self.assertEqual(outputs[0], outputs[1])
            if substituted:
                self.assertEqual(outputs[1], [{"value": substituted_value, "getters": 0}])
                spans = [span for request in capture.traces for resource in request.resource_spans
                         for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual(len(spans), 1 + extra_unscoped)
                scoped = [span for span in spans if any(a.key == "otelc.witness.scope_id" for a in span.attributes)]
                self.assertEqual(len(scoped), 1)
                self.assertEqual(next(a.value.string_value for a in scoped[0].attributes if a.key == "otelc.witness.role"), "wrapper")
                return
            self.assertEqual(outputs[1], [{"value": 8, "getters": 0}, {"error": True, "same": True, "getters": 0},
                {"value": 3, "getters": 0}, {"value": 4, "getters": 0}, {"value": 5, "getters": 0}])
            self.assertNotEqual(scopes[0]["cookie"], scopes[1]["cookie"])
            self.assertNotEqual(scopes[0]["scope_id"], scopes[1]["scope_id"])
            self.assertEqual([scope["sequence"] for scope in scopes], ["1", "2", "4"])
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 13 + extra_unscoped + 2 * (recursive_entries - 1))  # Four scoped calls plus unrelated calls.
            frozen_witness = json.loads((runtime / 'witness.json').read_text())
            for span in spans:
                self.assertEqual(span.dropped_attributes_count, 0, span.name)
                self.assertEqual(span.dropped_events_count, 0, span.name)
                self.assertEqual(span.dropped_links_count, 0, span.name)
                for event in span.events:
                    self.assertEqual(event.dropped_attributes_count, 0, span.name)
            for scope in scopes:
                actual = [span for span in spans if span.trace_id.hex() == scope["trace_id"]]
                self.assertEqual({span.span_id.hex() for span in actual}, set(scope["exported_ids"]))
                expected = {"scope_id": scope["scope_id"], "runtime_id": status["runtime_id"],
                    "context_cookie": scope["cookie"], "case_id": scope["case_id"],
                    "attempt_id": scope["attempt_id"], **{key: frozen_witness[key] for key in
                    ("build_sha256", "selection_sha256", "policy_sha256", "tooling_sha256")}}
                for span in actual:
                    attributes = {item.key: (item.value.WhichOneof('value'), item.value.string_value)
                                  for item in span.attributes}
                    for key, value in expected.items():
                        self.assertEqual(attributes.get('otelc.witness.' + key), ('string_value', value))
                roles = {next((a.value.string_value for a in span.attributes if a.key == "otelc.witness.role"), "function"): span
                         for span in actual}
                self.assertEqual(set(roles), {"wrapper", "target", "function"} | ({"recursive_target"} if int(scope["target_entries"]) > 1 else set()))
                self.assertEqual(roles["target"].parent_span_id.hex(), scope["root_span_id"])
                functions = [span for span in actual if not any(a.key == "otelc.witness.role" for a in span.attributes)]
                targets = [span for span in actual if any(a.key == "otelc.witness.role" and a.value.string_value in ("target", "recursive_target") for a in span.attributes)]
                self.assertEqual(len(functions), int(scope["target_entries"]))
                self.assertEqual(len(targets), int(scope["target_entries"]))
                self.assertTrue(all(span.parent_span_id in {target.span_id for target in targets} for span in functions))
                for span in targets:
                    if span != roles["target"]:
                        self.assertIn(span.parent_span_id, {function.span_id for function in functions})
                for role in ("wrapper", "target"):
                    probe = next(a.value.string_value for a in roles[role].attributes if a.key == "otelc.witness.probe_id")
                    self.assertEqual(probe, compiler_result["bindings"][role]["probe_id"])
            self.assertEqual(capture.calls(), 2 if live_metrics else 0,
                             "live metrics must remain independent of witness tracing")
