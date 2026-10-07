package quux.otelc.java;

import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.resources.Resource;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanLimits;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/** Private SDK contexts and bounded complete method trees. No application objects. */
final class TraceStore implements AutoCloseable {
  record Identity(SpanContext context, boolean sampled) {}
  static final Identity SUPPRESSED = new Identity(null, false);
  static final int MAX_RECORDS = 1048576;
  static final int MAX_BYTES = 16 * 1024 * 1024;
  private static final class Tree {
    final String root;
    final long origin;
    final long epoch;
    Map<String, Span> nodes = new LinkedHashMap<>();
    int active = 1;
    boolean closed;
    boolean invalid;
    long bytes;
    Tree(String root, long origin, long epoch) { this.root = root; this.origin = origin; this.epoch = epoch; }
  }
  private final Telemetry runtime;
  private final int maxActive;
  private final int maxSpans;
  private final int capacity;
  final int timeoutMs;
  final String endpoint;
  private final SdkTracerProvider provider;
  private final Tracer tracer;
  private final TraceExporter exporter;
  private final Map<String, Tree> roots = new LinkedHashMap<>();
  private final ArrayDeque<Tree> ready = new ArrayDeque<>();
  private final Map<String, Long> losses = new LinkedHashMap<>();
  private int retained;
  private long completed;
  private long sampledOut;
  private CompletableFuture<Boolean> activeFlush;

  static boolean enabled(Plan plan) { var traces = plan.section("traces"); return traces != null && traces.get("enabled").getAsBoolean(); }
  TraceStore(Telemetry runtime, Resource resource) {
    this.runtime = runtime;
    var plan = runtime.plan;
    double ratio;
    try {
      var configuredRatio = plan.section("traces").get("root_sample_ratio");
      if (!configuredRatio.isJsonPrimitive() || !configuredRatio.getAsJsonPrimitive().isNumber()) throw new IllegalArgumentException();
      ratio = configuredRatio.getAsDouble();
      maxActive = integer(plan, "traces", "max_active_traces");
      maxSpans = integer(plan, "traces", "max_spans_per_trace");
      capacity = integer(plan, "export", "max_queued_batches");
      timeoutMs = integer(plan, "trace_export", "timeout_ms");
      endpoint = plan.string("trace_export", "endpoint");
      var uri = URI.create(endpoint);
      boolean loopback = "localhost".equalsIgnoreCase(uri.getHost()) || "127.0.0.1".equals(uri.getHost()) || "[::1]".equals(uri.getHost());
      if (!Double.isFinite(ratio) || ratio < 0 || ratio > 1 || maxActive < 1 || maxActive > 65536 || maxSpans < 1 || maxSpans > 65536
          || (long)maxActive * maxSpans > MAX_RECORDS || capacity < 1 || capacity > 64 || timeoutMs < 1 || timeoutMs > 60000
          || !plan.string("trace_export", "protocol").equals("http/protobuf") || uri.getHost() == null || uri.getUserInfo() != null || uri.getQuery() != null || uri.getFragment() != null || uri.getPort() > 65535
          || !("https".equalsIgnoreCase(uri.getScheme()) || "http".equalsIgnoreCase(uri.getScheme()) && loopback)) throw new IllegalArgumentException();
    } catch (RuntimeException ignored) { throw new IllegalArgumentException("invalid resolved Java trace settings"); }
    provider = SdkTracerProvider.builder().setResource(resource).setSampler(Sampler.parentBased(Sampler.traceIdRatioBased(ratio)))
      .setSpanLimits(SpanLimits.builder().setMaxNumberOfAttributes(1).setMaxNumberOfEvents(0).setMaxNumberOfLinks(0).setMaxAttributeValueLength(1024).build()).build();
    tracer = provider.tracerBuilder("quux.otelc").setInstrumentationVersion("0.1.0").build();
    exporter = new TraceExporter(this);
  }
  private static int integer(Plan plan, String section, String name) {
    var value = plan.section(section).get(name);
    if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()) throw new IllegalArgumentException();
    return value.getAsBigDecimal().intValueExact();
  }
  synchronized Identity reject(Identity parent, String reason) {
    if (parent == null) lose(reason);
    else if (parent.sampled()) {
      var tree = roots.get(parent.context().getTraceId());
      if (tree != null && !tree.invalid) {
        tree.invalid = true; retained -= tree.nodes.size(); tree.nodes = new LinkedHashMap<>(); lose(reason);
      }
    }
    return SUPPRESSED;
  }
  synchronized Identity begin(Identity parent, String name, long started) {
    Tree tree = null;
    Context context = Context.root();
    long wall = TimeUnit.MILLISECONDS.toNanos(System.currentTimeMillis());
    if (parent != null) {
      if (!parent.sampled()) return SUPPRESSED;
      tree = roots.get(parent.context().getTraceId());
      if (tree == null || tree.invalid) return SUPPRESSED;
      if (tree.nodes.size() >= maxSpans || retained >= MAX_RECORDS) return reject(parent, "span_capacity");
      context = context.with(Span.wrap(parent.context())); wall = tree.epoch + Math.max(0, started - tree.origin);
    }
    var span = tracer.spanBuilder(name).setParent(context).setStartTimestamp(wall, TimeUnit.NANOSECONDS).setAttribute("code.function.name", name).startSpan();
    var identity = span.getSpanContext();
    if (!identity.isValid()) return reject(parent, "invalid");
    if (!identity.isSampled()) { sampledOut++; return SUPPRESSED; }
    if (parent == null) {
      if (roots.size() >= maxActive || retained >= MAX_RECORDS) return reject(null, "trace_capacity");
      if (roots.containsKey(identity.getTraceId())) return reject(null, "invalid");
      tree = new Tree(identity.getSpanId(), started, wall); roots.put(identity.getTraceId(), tree);
    } else {
      if (tree.nodes.containsKey(identity.getSpanId())) return reject(parent, "invalid");
      tree.active++;
    }
    tree.nodes.put(identity.getSpanId(), span); tree.bytes += name.getBytes(StandardCharsets.UTF_8).length * 2L + 256; retained++;
    return new Identity(identity, true);
  }
  synchronized void finish(Identity identity, long ended, boolean escaped) {
    if (identity == null || !identity.sampled()) return;
    var tree = roots.get(identity.context().getTraceId());
    if (tree == null) return;
    if (!tree.invalid) {
      var span = tree.nodes.get(identity.context().getSpanId());
      if (span == null || ((ReadableSpan)span).hasEnded()) return;
      if (escaped) span.setStatus(StatusCode.ERROR, "escaping unwind");
      span.end(Math.max(((ReadableSpan)span).toSpanData().getStartEpochNanos(), tree.epoch + Math.max(0, ended - tree.origin)), TimeUnit.NANOSECONDS);
    }
    tree.active--;
    if (identity.context().getSpanId().equals(tree.root)) tree.closed = true;
    if (tree.active != 0 || !tree.closed) return;
    roots.remove(identity.context().getTraceId());
    if (tree.invalid) return;
    if (ready.size() >= capacity) { retained -= tree.nodes.size(); lose("queue_capacity"); }
    else { completed++; ready.addLast(tree); }
  }
  private void lose(String reason) { losses.merge(reason, 1L, Long::sum); }
  synchronized Map<String, Long> losses() { return new LinkedHashMap<>(losses); }
  synchronized Map<String, Object> report() { return Map.of("completed_trees", completed, "sampled_out_roots", sampledOut, "active_trees", roots.size(), "queued_trees", ready.size(), "losses", losses()); }
  synchronized void shutdownPending() {
    for (var tree : roots.values()) { retained -= tree.nodes.size(); if (!tree.invalid) lose("incomplete"); }
    roots.clear();
  }
  synchronized CompletableFuture<Boolean> flush() {
    if (activeFlush != null && !activeFlush.isDone()) return activeFlush;
    activeFlush = exportNext(System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs), capacity, true);
    return activeFlush;
  }
  private CompletableFuture<Boolean> exportNext(long deadline, int remaining, boolean success) {
    Tree tree;
    synchronized (this) {
      if (remaining == 0 || ready.isEmpty()) return CompletableFuture.completedFuture(success);
      tree = ready.removeFirst(); retained -= tree.nodes.size();
      if (tree.bytes > MAX_BYTES) { lose("batch_bytes"); return exportNext(deadline, remaining - 1, success); }
    }
    var spans = tree.nodes.values().stream().map(span -> ((ReadableSpan)span).toSpanData()).toList();
    return exporter.export(spans, Math.min(deadline, runtime.shutdownDeadline)).handle((accepted, failure) -> {
      boolean good = failure == null && Boolean.TRUE.equals(accepted);
      if (!good) runtime.exportLoss.incrementAndGet();
      return good;
    }).thenCompose(good -> exportNext(deadline, remaining - 1, success && good));
  }
  @Override public void close() { exporter.close(); provider.shutdown(); }
}
