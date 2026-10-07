package quux.otelc.java;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpServer;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceRequest;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceResponse;
import io.opentelemetry.proto.collector.trace.v1.ExportTracePartialSuccess;
import io.opentelemetry.proto.trace.v1.Span;
import java.lang.reflect.InvocationTargetException;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class SpansTest {
  @TempDir Path directory;
  static JsonObject policy(String metrics, String traces) {
    var data = AgentTest.policy(metrics);
    data.getAsJsonObject("export").addProperty("max_queued_batches", 8);
    data.getAsJsonObject("export").addProperty("timeout_ms", 2000);
    data.getAsJsonObject("runtime").addProperty("shutdown_timeout_ms", 5000);
    data.add("traces", JsonParser.parseString("""
      {"enabled":true,"root_sample_ratio":1.0,"max_active_traces":8,"max_spans_per_trace":32}
      """));
    data.add("trace_export", JsonParser.parseString("""
      {"endpoint":"ENDPOINT","protocol":"http/protobuf","timeout_ms":2000}
      """.replace("ENDPOINT", traces)));
    return data;
  }
  static final class Receiver implements AutoCloseable {
    final HttpServer server;
    final java.util.concurrent.ExecutorService executor = Executors.newCachedThreadPool(Thread.ofPlatform().daemon().factory());
    final List<ExportTraceServiceRequest> requests = new CopyOnWriteArrayList<>();
    final List<byte[]> bodies = new CopyOnWriteArrayList<>();
    final AtomicInteger attempts = new AtomicInteger();
    final CountDownLatch reached = new CountDownLatch(1);
    final CountDownLatch release = new CountDownLatch(1);
    Receiver(int code, byte[] response, boolean transientFailure, boolean stalled) throws Exception {
      server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
      server.setExecutor(executor);
      server.createContext("/custom/traces", exchange -> {
        try {
          byte[] body = exchange.getRequestBody().readAllBytes(); bodies.add(body);
          requests.add(ExportTraceServiceRequest.parseFrom(body));
          int attempt = attempts.incrementAndGet(); reached.countDown();
          if (stalled) release.await(5, TimeUnit.SECONDS);
          int status = transientFailure && attempt == 1 ? 503 : code;
          exchange.sendResponseHeaders(status, response.length == 0 ? -1 : response.length);
          if (response.length > 0) exchange.getResponseBody().write(response);
        } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        finally { exchange.close(); }
      });
      server.start();
    }
    Receiver() throws Exception { this(200, new byte[0], false, false); }
    String endpoint() { return "http://127.0.0.1:" + server.getAddress().getPort() + "/custom/traces"; }
    List<Span> spans() { return requests.stream().flatMap(request -> request.getResourceSpansList().stream()).flatMap(resource -> resource.getScopeSpansList().stream()).flatMap(scope -> scope.getSpansList().stream()).toList(); }
    @Override public void close() { release.countDown(); server.stop(0); executor.shutdownNow(); }
  }
  @Test void wovenOriginalMethodsPreserveReturnsExceptionsAndRecursiveParents() throws Exception {
    var helper = new AgentTest(); helper.directory = directory;
    String source = """
      package example;
      public class TraceApp {
        public static int recursive(int n) { return n==0?0:1+recursive(n-1); }
        public static Object object(Object o) { return o; }
        public static void escaping(Throwable error) throws Throwable { throw error; }
        public static int caught() { try { throw new Error("caught"); } catch(Error e) { return 7; } }
      }
      """;
    byte[] original = helper.compile("TraceApp", source);
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(); var runtime = new Telemetry(new Plan(policy(metrics.endpoint(), traces.endpoint())))) {
      Probes.runtime = runtime;
      try {
        var loader = new AgentTest.Loader();
        var weaver = new Weaver(runtime.plan, new Sources(directory, runtime.plan), runtime, null);
        var type = loader.define(weaver.weave(original, loader));
        assertEquals(3, type.getMethod("recursive", int.class).invoke(null, 3));
        var value = new Object(); assertSame(value, type.getMethod("object", Object.class).invoke(null, value));
        var error = new IllegalArgumentException("original private payload");
        assertSame(error, assertThrows(InvocationTargetException.class, () -> type.getMethod("escaping", Throwable.class).invoke(null, error)).getCause());
        assertEquals(7, type.getMethod("caught").invoke(null));
        assertEquals(source, Files.readString(directory.resolve("TraceApp.java")));
        assertArrayEquals(original, Files.readAllBytes(directory.resolve("classes/example/TraceApp.class")));
        runtime.close();
        assertEquals(7, runtime.count()); assertEquals(0, runtime.exportLoss.get()); assertEquals(true, runtime.report().get("export_finished"));
        var spans = traces.spans(); assertEquals(7, spans.size());
        var recursive = spans.stream().filter(span -> span.getName().contains(".recursive(")).toList(); assertEquals(4, recursive.size());
        for (int i = 0; i < recursive.size(); i++) {
          var span = recursive.get(i);
          assertEquals(16, span.getTraceId().size()); assertEquals(8, span.getSpanId().size());
          assertTrue(span.getTraceId().toByteArray()[0] != 0 || !span.getTraceId().equals(com.google.protobuf.ByteString.copyFrom(new byte[16])));
          assertTrue(span.getEndTimeUnixNano() >= span.getStartTimeUnixNano());
          if (i == 0) assertTrue(span.getParentSpanId().isEmpty());
          else { assertEquals(recursive.get(i-1).getSpanId(), span.getParentSpanId()); assertEquals(recursive.get(0).getTraceId(), span.getTraceId()); assertTrue(span.getEndTimeUnixNano() <= recursive.get(i-1).getEndTimeUnixNano()); }
        }
        assertEquals(2, spans.stream().filter(span -> span.getName().contains("escaping")).findFirst().orElseThrow().getStatus().getCodeValue());
        assertEquals(0, spans.stream().filter(span -> span.getName().contains("caught")).findFirst().orElseThrow().getStatus().getCodeValue());
        assertFalse(traces.requests.toString().contains("original private payload"));
        var resource = traces.requests.getFirst().getResourceSpans(0);
        assertTrue(resource.getResource().getAttributesList().stream().anyMatch(attribute -> attribute.getKey().equals("service.name") && attribute.getValue().getStringValue().equals("java-test")));
        assertEquals("quux.otelc", resource.getScopeSpans(0).getScope().getName());
      } finally { Probes.runtime = null; }
    }
  }
  @Test void virtualThreadContextsStayIsolatedAndMetricsAdmissionRemainsSticky() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("metrics").addProperty("enabled", false);
      try (var runtime = new Telemetry(new Plan(data))) {
        long root = runtime.enter("root"); runtime.enabled = true;
        long child = runtime.enter("child"); runtime.exit(child, false);
        var result = new ArrayList<Throwable>();
        var thread = Thread.ofVirtual().start(() -> { try { runtime.exit(runtime.enter("independent"), false); } catch (Throwable failure) { result.add(failure); } });
        thread.join(); assertTrue(result.isEmpty());
        runtime.exit(root, false); runtime.close();
        assertEquals(2, runtime.count()); assertEquals(0, runtime.functions.get("root").calls.get());
        var spans = traces.spans(); assertEquals(3, spans.size());
        var rootSpan = spans.stream().filter(span -> span.getName().equals("root")).findFirst().orElseThrow();
        var childSpan = spans.stream().filter(span -> span.getName().equals("child")).findFirst().orElseThrow();
        var independent = spans.stream().filter(span -> span.getName().equals("independent")).findFirst().orElseThrow();
        assertEquals(rootSpan.getSpanId(), childSpan.getParentSpanId()); assertFalse(rootSpan.getTraceId().equals(independent.getTraceId())); assertTrue(independent.getParentSpanId().isEmpty());
      }
    }
  }
  @Test void samplingZeroIsInheritedAndUnfinishedUnobservedCallsAreNotLosses() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("metrics").addProperty("enabled", false); data.getAsJsonObject("traces").addProperty("root_sample_ratio", 0);
      try (var runtime = new Telemetry(new Plan(data))) {
        runtime.enter("root"); runtime.enter("child"); runtime.close();
        assertTrue(traces.spans().isEmpty()); assertEquals(0, runtime.losses.get("incomplete").get());
        assertEquals(1L, runtime.traces.report().get("sampled_out_roots")); assertTrue(runtime.traces.losses().isEmpty());
      }
    }
  }
  @Test void everySharedLimitInvalidatesTheWholeAffectedTree() throws Exception {
    for (String setting : List.of("max_functions", "max_active_calls", "max_spans_per_trace")) {
      try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
        var data = policy(metrics.endpoint(), traces.endpoint());
        data.getAsJsonObject(setting.equals("max_spans_per_trace") ? "traces" : "runtime").addProperty(setting, 1);
        try (var runtime = new Telemetry(new Plan(data))) {
          long root = runtime.enter("root"), child = runtime.enter("child"); runtime.exit(child, false); runtime.exit(root, false); runtime.close();
          assertTrue(traces.spans().isEmpty());
          String reason = setting.equals("max_functions") ? "function_capacity" : setting.equals("max_active_calls") ? "active_call_capacity" : "span_capacity";
          assertEquals(1L, runtime.traces.losses().get(reason));
        }
      }
    }
  }
  @Test void weavingDoesNotSilentlyOmitCapacityRejectedChildren() throws Exception {
    var helper = new AgentTest(); helper.directory = directory;
    byte[] original = helper.compile("TraceCapacity", "package example; public class TraceCapacity { public static int root(){ return child(); } public static int child(){ return 42; } }");
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("runtime").addProperty("max_functions", 1);
      var include = data.getAsJsonObject("function_matchers").getAsJsonArray("include"); include.set(0, new com.google.gson.JsonPrimitive("(?-u).*\\.(root|child)\\(\\)"));
      try (var runtime = new Telemetry(new Plan(data))) {
        Probes.runtime = runtime;
        try {
          var loader = new AgentTest.Loader();
          var type = loader.define(new Weaver(runtime.plan, new Sources(directory, runtime.plan), runtime, null).weave(original, loader));
          assertEquals(42, type.getMethod("root").invoke(null)); runtime.close();
          assertTrue(traces.spans().isEmpty(), "capacity-rejected child produced a partial parent tree");
          assertEquals(Map.of("function_capacity", 1L), runtime.traces.losses());
        } finally { Probes.runtime = null; }
      }
    }
  }
  @Test void rejectedRootsSuppressDescendantsAndRestoreTheCallerContext() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("runtime").addProperty("max_functions", 1);
      try (var runtime = new Telemetry(new Plan(data))) {
        assertTrue(runtime.register("known"));
        long rejected = runtime.enter("unknown");
        long nested = runtime.enter("another_unknown");
        runtime.exit(runtime.enter("known"), false);
        runtime.exit(nested, false);
        runtime.exit(runtime.enter("known"), false);
        runtime.exit(rejected, false);
        runtime.exit(runtime.enter("known"), false); runtime.close();
        assertEquals(List.of("known"), traces.spans().stream().map(Span::getName).toList());
        assertEquals(Map.of("function_capacity", 1L), runtime.traces.losses());
      }
    }
  }
  @Test void rootQueueAndIncompleteLimitsAreVisibleAndReusable() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint());
      data.getAsJsonObject("traces").addProperty("max_active_traces", 1); data.getAsJsonObject("export").addProperty("max_queued_batches", 1);
      try (var runtime = new Telemetry(new Plan(data))) {
        long root = runtime.enter("root");
        var thread = Thread.ofPlatform().start(() -> runtime.exit(runtime.enter("overflow"), false)); thread.join();
        runtime.exit(root, false); runtime.exit(runtime.enter("queue_overflow"), false); runtime.enter("unfinished");
        runtime.close();
        assertEquals(1, traces.spans().size());
        assertEquals(Map.of("trace_capacity", 1L, "queue_capacity", 1L, "incomplete", 1L), runtime.traces.losses());
      }
    }
  }
  @Test void invalidatedPayloadsDoNotLeakOrProduceNewDescendantRoots() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(); var runtime = new Telemetry(new Plan(policy(metrics.endpoint(), traces.endpoint())))) {
      long root = runtime.enter("root");
      runtime.traces.reject(runtime.pending.get(root).trace(), "test_rejection");
      long child = runtime.enter("child"); runtime.exit(child, false); runtime.exit(root, false);
      runtime.exit(root, false); runtime.exit(0, false);
      runtime.exit(runtime.enter("new_root"), false); runtime.close();
      assertEquals(List.of("new_root"), traces.spans().stream().map(Span::getName).toList());
      assertEquals(Map.of("test_rejection", 1L), runtime.traces.losses());
    }
  }
  @Test void completedOversizeTreeIsDiscardedBeforeEncoding() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("traces").addProperty("max_active_traces", 1); data.getAsJsonObject("traces").addProperty("max_spans_per_trace", 10000);
      try (var runtime = new Telemetry(new Plan(data))) {
        long root = runtime.enter("root"); String name = "x".repeat(1024);
        for (int i = 0; i < 8192; i++) runtime.exit(runtime.enter(name), false);
        runtime.exit(root, false); runtime.close();
        assertTrue(traces.requests.isEmpty()); assertEquals(Map.of("batch_bytes", 1L), runtime.traces.losses());
      }
    }
  }
  @Test void realTransportRetriesTransientFailuresAndRejectsPermanentPartialMalformedOrRedirectedSuccess() throws Exception {
    byte[] partial = ExportTraceServiceResponse.newBuilder().setPartialSuccess(ExportTracePartialSuccess.newBuilder().setRejectedSpans(1)).build().toByteArray();
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(200, new byte[0], true, false); var runtime = new Telemetry(new Plan(policy(metrics.endpoint(), traces.endpoint())))) {
      runtime.exit(runtime.enter("retry"), false); runtime.close(); assertEquals(0, runtime.exportLoss.get()); assertEquals(2, traces.attempts.get()); assertArrayEquals(traces.bodies.getFirst(), traces.bodies.getLast());
    }
    for (int status : new int[] {200, 401, 202, 302}) {
      var bodies = status == 200 ? List.of(partial, new byte[] {(byte)255}, new byte[65537]) : List.of(new byte[0]);
      for (byte[] body : bodies) {
        try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(status, body, false, false); var runtime = new Telemetry(new Plan(policy(metrics.endpoint(), traces.endpoint())))) {
          runtime.exit(runtime.enter("rejected"), false); runtime.close(); assertEquals(1, traces.attempts.get()); assertEquals(1, runtime.exportLoss.get());
        }
      }
    }
  }
  @Test void stalledTraceExportDoesNotExtendApplicationShutdown() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(200, new byte[0], false, true); var runtime = new Telemetry(new Plan(policy(metrics.endpoint(), traces.endpoint())))) {
      runtime.exit(runtime.enter("stalled"), false);
      var exporting = runtime.traces.flush(); assertTrue(traces.reached.await(3, TimeUnit.SECONDS));
      runtime.plan.section("runtime").addProperty("shutdown_timeout_ms", 120);
      long started = System.nanoTime(); runtime.close();
      assertTrue(System.nanoTime() - started < TimeUnit.SECONDS.toNanos(1));
      assertEquals(false, runtime.report().get("export_finished"));
      traces.release.countDown(); exporting.get(3, TimeUnit.SECONDS);
    }
  }
  @Test void failedTraceExportHealthReachesMetricsWithoutAnotherApplicationCall() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver(401, new byte[0], false, false)) {
      var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("export").addProperty("interval_ms", 20);
      try (var runtime = new Telemetry(new Plan(data))) {
        runtime.exit(runtime.enter("one"), false);
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (metrics.requests.stream().flatMap(request -> request.getResourceMetricsList().stream()).flatMap(resource -> resource.getScopeMetricsList().stream()).flatMap(scope -> scope.getMetricsList().stream()).filter(metric -> metric.getName().equals("otelc.export.dropped_batches")).flatMap(metric -> metric.getSum().getDataPointsList().stream()).noneMatch(point -> point.getAsInt() == 1)) {
          assertTrue(System.nanoTime() < deadline, "trace export loss never reached periodic metrics"); Thread.sleep(2);
        }
        assertEquals(1, traces.attempts.get()); runtime.close();
      }
    }
  }
  @Test void forgedSettingsAndSignalHeadersRespectTheSharedPolicy() throws Exception {
    try (var metrics = new AgentTest.Receiver(); var traces = new Receiver()) {
      for (String setting : List.of("max_active_traces", "max_spans_per_trace", "max_queued_batches", "timeout_ms")) {
        var data = policy(metrics.endpoint(), traces.endpoint());
        data.getAsJsonObject(setting.equals("timeout_ms") ? "trace_export" : setting.equals("max_queued_batches") ? "export" : "traces").addProperty(setting, 0);
        assertEquals("invalid resolved Java trace settings", assertThrows(IllegalArgumentException.class, () -> new Telemetry(new Plan(data))).getMessage());
      }
      for (String endpoint : List.of("http://private.example/secret", "https://user:secret@example.invalid", "https://example.invalid?private=value", "https://example.invalid#private", "https://example.invalid:65536")) {
        var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("trace_export").addProperty("endpoint", endpoint);
        assertEquals("invalid resolved Java trace settings", assertThrows(IllegalArgumentException.class, () -> new Telemetry(new Plan(data))).getMessage());
      }
      var fractional = policy(metrics.endpoint(), traces.endpoint()); fractional.getAsJsonObject("traces").addProperty("max_active_traces", 1.5);
      assertThrows(IllegalArgumentException.class, () -> new Telemetry(new Plan(fractional)));
      for (String ratio : List.of("0.5", "NaN", "Infinity")) {
        var data = policy(metrics.endpoint(), traces.endpoint()); data.getAsJsonObject("traces").addProperty("root_sample_ratio", ratio);
        assertThrows(IllegalArgumentException.class, () -> new Telemetry(new Plan(data)));
      }
      var impossible = policy(metrics.endpoint(), traces.endpoint()); impossible.getAsJsonObject("traces").addProperty("max_active_traces", 65536); impossible.getAsJsonObject("traces").addProperty("max_spans_per_trace", 65536);
      assertThrows(IllegalArgumentException.class, () -> new Telemetry(new Plan(impossible)));
    }
    assertEquals(Map.of(), Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x-private=ignored", "OTEL_EXPORTER_OTLP_TRACES_HEADERS", ""), "OTEL_EXPORTER_OTLP_TRACES_HEADERS"));
    assertThrows(IllegalArgumentException.class, () -> Exporter.headers(Map.of("OTEL_EXPORTER_OTLP_HEADERS", "x=" + "s".repeat(8192))));
  }
}
