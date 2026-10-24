"""Real function probes preserve exception identity and avoid application callbacks."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import benchmark, control, health, java, prepared_java
from otelc.config import Policy
from test_java_integration import Capture, batch, wait_for_count
from test_prepared_java import binding

SOURCE = r'''
import java.io.*;
import java.util.concurrent.*;
public final class FailureApp {
  static int callbacks;
  static Throwable expected;
  static CompletableFuture<Integer> pending;
  static CompletableFuture<Integer> returned;
  static boolean caughtSame;
  static final class Problem extends RuntimeException {
    final boolean dangerous;
    Problem(boolean dangerous) { this.dangerous = dangerous; }
    void callback() { callbacks++; if (dangerous) throw new AssertionError("getter called"); }
    @Override public String getMessage() { callback(); return "message"; }
    @Override public String getLocalizedMessage() { callback(); return "localized"; }
    @Override public synchronized Throwable getCause() { callback(); return null; }
    @Override public String toString() { callback(); return "text"; }
    @Override public void printStackTrace(PrintWriter out) { callback(); }
    @Override public void printStackTrace(PrintStream out) { callback(); }
    @Override public StackTraceElement[] getStackTrace() { callback(); return new StackTraceElement[0]; }
  }
  static final class WrappedProblem extends CompletionException {
    WrappedProblem() { super("wrapper", null); }
    @Override public synchronized Throwable getCause() { callbacks++; throw new AssertionError("cause called"); }
    @Override public String getMessage() { callbacks++; throw new AssertionError("message called"); }
    @Override public String toString() { callbacks++; throw new AssertionError("format called"); }
  }
  static RuntimeException failure(int kind) {
    return switch(kind) {
      case 0 -> new Problem(false);
      case 1 -> new Problem(true);
      case 2 -> new RuntimeException("outer", new Problem(false));
      case 3 -> new RuntimeException("outer", new Problem(true));
      case 4 -> new WrappedProblem();
      default -> new CompletionException("wrapper", new Problem(true));
    };
  }
  static void workload(int kind) { RuntimeException error = failure(kind); expected = error; throw error; }
  static CompletableFuture<Integer> asyncWork() { pending = new CompletableFuture<>(); return pending; }
  public static void main(String[] args) throws Exception {
    var input = new BufferedReader(new InputStreamReader(System.in));
    for (String line; (line = input.readLine()) != null && !line.equals("quit");) {
      boolean same = true;
      if (line.equals("start")) returned = asyncWork();
      else if (line.equals("finish")) {
        RuntimeException error = failure(3);
        caughtSame = false;
        returned.whenComplete((value, caught) -> { caughtSame = caught == error; });
        pending.completeExceptionally(error);
        same = returned == pending && caughtSame;
      } else {
        try { workload(Integer.parseInt(line)); }
        catch (RuntimeException error) { same = error == expected; }
      }
      System.out.println("{\"callbacks\":" + callbacks + ",\"same\":" + same + "}");
    }
  }
}
'''


class ExceptionIntegrationTests(unittest.TestCase):
    source = SOURCE

    def test_throwable_callbacks_identity_async_completion_and_live_states(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "FailureApp.java"
            source.write_text(self.source)
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "FailureApp", "classpath": ["classes"],
                "bindings": {path.stem: binding(source, path, "src/FailureApp.java") for path in classes.glob("*.class")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]},
                "functions": {"include": ["FailureApp.workload(*)", "FailureApp.asyncWork()"]},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "metrics": {"collection": False, "emission": False},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain, diagnostic_profile=True)
                with (runtime / "stderr").open("w+") as errors:
                    process = subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                    try:
                        if not plain:
                            control.wait_ready(runtime / "control.sock", process.pid)
                        for mode, expected in (("both_on", 7), ("collection_only", 14),
                                               ("emission_only", 14), ("both_off", 14), ("both_on", 21)):
                            if not plain:
                                state = benchmark.switch_state(runtime / "control.sock", process, mode)
                                self.assertEqual(state["pid"], process.pid)
                            for kind in range(6):
                                self.assertEqual(batch(process, kind), {"callbacks": 0, "same": True})
                            before = None if plain else control.request(runtime / "control.sock", "status")["completed"]
                            self.assertEqual(batch(process, "start"), {"callbacks": 0, "same": True})
                            if not plain:
                                self.assertEqual(control.request(runtime / "control.sock", "status")["completed"], before,
                                                 "an unresolved future is not a completed operation")
                                self.assertEqual(control.request(runtime / "control.sock", "health")["span_accounting"]["sampled_active"], "1")
                            self.assertEqual(batch(process, "finish"), {"callbacks": 0, "same": True})
                            if not plain:
                                self.assertEqual(control.request(runtime / "control.sock", "flush")["completed"], expected)
                        if not plain:
                            self.assertEqual(wait_for_count(capture.calls, 18), 18)
                        process.stdin.write("quit\n")
                        process.stdin.flush()
                        self.assertEqual(process.wait(timeout=15), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                        process.stdin.close()
                        process.stdout.close()
                    errors.seek(0)
                    self.assertEqual(errors.read(), "")
                launch.verify_inputs()
                if not plain:
                    report = health.read(runtime / "health.json")
                    self.assertEqual(report["span_accounting"]["queue_dropped"], "0")
                    self.assertEqual(report["span_accounting"]["sampled_active"], "0")
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(len(spans), 35)
            for span in spans:
                self.assertEqual(span.status.code, 2)
                self.assertEqual(len(span.events), 1)
                self.assertEqual(span.events[0].name, "exception")
                attributes = {item.key: item.value.string_value for item in span.events[0].attributes}
                self.assertEqual(set(attributes), {"exception.type"})
                self.assertIn(attributes["exception.type"], {"FailureApp$Problem", "FailureApp$WrappedProblem",
                    "java.lang.RuntimeException", "java.util.concurrent.CompletionException"})


class DirectCompletionIntegrationTests(ExceptionIntegrationTests):
    """A top-level custom throwable must stay untouched in every live metric state."""

    source = SOURCE.replace('RuntimeException error = failure(3);', 'RuntimeException error = failure(1);').replace(
        'returned.whenComplete((value, caught) -> { caughtSame = caught == error; });',
        'returned.handle((value, caught) -> { caughtSame = caught == error; return null; });')
