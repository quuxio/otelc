package quux.otelc.java;

import com.google.gson.GsonBuilder;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.metrics.LongCounter;
import io.opentelemetry.api.metrics.DoubleHistogram;
import io.opentelemetry.sdk.metrics.Aggregation;
import io.opentelemetry.sdk.metrics.InstrumentSelector;
import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.View;
import io.opentelemetry.sdk.metrics.export.PeriodicMetricReader;
import io.opentelemetry.sdk.resources.Resource;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.locks.ReentrantLock;

/** Bounded pending observations and language-native SDK metrics. */
public final class Telemetry implements AutoCloseable {
  final Plan plan;
  final long timeoutMs;
  final TraceStore traces;
  final TaskContext tasks;
  volatile long shutdownDeadline = Long.MAX_VALUE;
  final AtomicLong exportLoss = new AtomicLong();
  final Map<String, AtomicLong> losses = new LinkedHashMap<>();
  final ConcurrentHashMap<String, Function> functions = new ConcurrentHashMap<>();
  final ConcurrentHashMap<Long, Frame> pending = new ConcurrentHashMap<>();
  private final AtomicLong next = new AtomicLong();
  final AtomicBoolean closed = new AtomicBoolean();
  private final AtomicBoolean sdkStopping = new AtomicBoolean();
  private final CompletableFuture<Boolean> sdkStopped = new CompletableFuture<>();
  final ReentrantLock observations = new ReentrantLock();
  final ThreadLocal<Long> current = ThreadLocal.withInitial(() -> 0L);
  private volatile boolean discardCompletions;
  volatile boolean enabled;
  private volatile boolean exportFinished;
  private final Exporter exporter;
  private final SdkMeterProvider provider;
  private final LongCounter calls;
  private final LongCounter unwinds;
  private final DoubleHistogram duration;
  private Control control;
  record Frame(String name, long started, boolean metrics, TraceStore.Identity trace, long previous) {}
  static final class Function {
    final Attributes attributes;
    final AtomicLong calls = new AtomicLong();
    final AtomicLong unwinds = new AtomicLong();
    Function(String name) { attributes = Attributes.builder().put("code.function.name", name).build(); }
  }
  public Telemetry(Plan plan) {
    this.plan = plan;
    enabled = plan.bool("metrics", "enabled");
    timeoutMs = Math.min(plan.integer("export", "timeout_ms"), plan.integer("runtime", "shutdown_timeout_ms"));
    for (String reason : new String[] {"function_capacity", "active_call_capacity", "incomplete", "invalid", "unsupported_class"}) losses.put(reason, new AtomicLong());
    var attributes = Attributes.builder().put("service.name", plan.string("resource", "service_name")).put("service.version", plan.string("resource", "service_version")).put("service.instance.id", Long.toString(ProcessHandle.current().pid()));
    plan.section("resource").getAsJsonObject("attributes").entrySet().forEach(entry -> attributes.put(entry.getKey(), entry.getValue().getAsString()));
    var resource = Resource.create(attributes.build());
    traces = TraceStore.enabled(plan) ? new TraceStore(this, resource) : null;
    var propagation=plan.section("propagation");
    tasks=propagation!=null && propagation.get("tasks").getAsBoolean() ? new TaskContext(this) : null;
    exporter = new Exporter(this);
    var reader = PeriodicMetricReader.builder(exporter).setInterval(Duration.ofMillis(plan.integer("export", "interval_ms"))).build();
    var boundaries = plan.section("metrics").getAsJsonArray("histogram_boundaries_seconds").asList().stream().map(value -> value.getAsDouble()).toList();
    var builder = SdkMeterProvider.builder().setResource(resource).registerMetricReader(reader);
    for (String name : new String[] {"otelc.function.calls", "otelc.function.unwinds", "otelc.function.duration"}) {
      var view = View.builder().setCardinalityLimit(plan.integer("runtime", "max_functions") + 1);
      if (name.endsWith("duration")) view.setAggregation(Aggregation.explicitBucketHistogram(boundaries));
      builder.registerView(InstrumentSelector.builder().setName(name).build(), view.build());
    }
    provider = builder.build();
    var meter = provider.meterBuilder("quux.otelc").setInstrumentationVersion("0.1.0").build();
    calls = meter.counterBuilder("otelc.function.calls").setUnit("{call}").build();
    unwinds = meter.counterBuilder("otelc.function.unwinds").setUnit("{observation}").build();
    duration = meter.histogramBuilder("otelc.function.duration").setUnit("s").build();
    meter.counterBuilder("otelc.runtime.dropped_observations").setUnit("{observation}").buildWithCallback(observer -> losses.forEach((reason, count) -> observer.record(count.get(), Attributes.builder().put("reason", reason).build())));
    meter.counterBuilder("otelc.export.dropped_batches").setUnit("{batch}").buildWithCallback(observer -> observer.record(exportLoss.get()));
    if (traces != null) meter.counterBuilder("otelc.trace.dropped_trees").setUnit("{tree}").buildWithCallback(observer -> traces.losses().forEach((reason, count) -> observer.record(count, Attributes.builder().put("reason", reason).build())));
  }
  boolean register(String name) {
    observations.lock();
    try {
      if (functions.containsKey(name)) return true;
      if (functions.size() >= plan.integer("runtime", "max_functions") || name.getBytes(StandardCharsets.UTF_8).length > 1024) { lose("function_capacity"); return false; }
      functions.put(name, new Function(name)); return true;
    } finally { observations.unlock(); }
  }
  long enter(String name) {
    if ((!enabled && traces == null) || closed.get()) return 0;
    observations.lock();
    try {
      if ((!enabled && traces == null) || closed.get()) return 0;
      if(tasks!=null) tasks.observed();
      long previous = traces == null ? 0 : current.get();
      var parent = parent();
      if (!register(name)) { if (traces != null) traces.reject(parent, "function_capacity"); return suppress(previous); }
      if (pending.size() >= plan.integer("runtime", "max_active_calls")) { lose("active_call_capacity"); if (traces != null) traces.reject(parent, "active_call_capacity"); return suppress(previous); }
      long token = next.incrementAndGet();
      if (token <= 0 || token == Long.MAX_VALUE) { lose("invalid"); if (traces != null) traces.reject(parent, "invalid"); return suppress(previous); }
      long started = System.nanoTime();
      var trace = traces == null ? null : traces.begin(parent, name, started);
      pending.put(token, new Frame(name, started, enabled, trace, previous));
      if (traces != null) current.set(token);
      return token;
    } finally { observations.unlock(); }
  }
  TraceStore.Identity parent() {
    long token=current.get(); var caller=pending.get(token);
    return token==0 ? tasks==null ? null : tasks.parent() : caller==null ? TraceStore.SUPPRESSED : caller.trace();
  }
  private long suppress(long previous) {
    if (traces == null) return 0;
    // Store rejected scope restoration in the bytecode's primitive local, outside bounded maps.
    current.set(Long.MIN_VALUE);
    return previous == Long.MIN_VALUE ? Long.MIN_VALUE : -previous - 1;
  }
  void exit(long token, boolean escaped) {
    if (token == 0) return;
    long ended = System.nanoTime();
    observations.lock();
    try {
      if (token < 0) {
        long previous = token == Long.MIN_VALUE ? Long.MIN_VALUE : -token - 1;
        if (previous == 0 || closed.get()) current.remove(); else current.set(previous);
        return;
      }
      var frame = pending.get(token); if (frame == null) return;
      var function = functions.get(frame.name());
      if (frame.metrics()) {
        calls.add(1, function.attributes); duration.record((ended - frame.started()) / 1e9, function.attributes);
        if (escaped) unwinds.add(1, function.attributes);
      }
      pending.remove(token);
      if (traces != null) {
        if (current.get() == token) { if (frame.previous() == 0) current.remove(); else current.set(frame.previous()); }
        if (discardCompletions) traces.reject(frame.trace(), "incomplete");
        traces.finish(frame.trace(), ended, escaped);
      }
      if (discardCompletions) { if (frame.metrics() || frame.trace() != null && frame.trace().sampled()) lose("incomplete"); return; }
      if (frame.metrics()) { function.calls.incrementAndGet(); if (escaped) function.unwinds.incrementAndGet(); }
    } finally { observations.unlock(); }
  }
  void lose(String reason) { losses.get(reason).incrementAndGet(); }
  void bindControl() throws IOException { if (plan.controlSocket() != null) control = new Control(this); }
  long count() { return functions.values().stream().mapToLong(value -> value.calls.get()).sum(); }
  Map<String, Object> report() throws IOException {
    var result = new LinkedHashMap<String, Object>(); result.put("schema_version", 1); result.put("language", "java"); result.put("pid", ProcessHandle.current().pid()); result.put("export_finished", exportFinished); result.put("function_calls", count());
    var values = new LinkedHashMap<String, Object>(); functions.forEach((name, value) -> values.put(name, Map.of("count", value.calls.get(), "unwinds", value.unwinds.get()))); result.put("functions", values);
    var lost = new LinkedHashMap<String, Long>(); losses.forEach((name, value) -> lost.put(name, value.get())); lost.compute("incomplete", (name, value) -> value + incomplete()); result.put("losses", lost); result.put("export_loss", exportLoss.get());
    if (traces != null) result.put("traces", traces.report());
    String filename = System.getenv("OTELC_REPORT_PATH"); if (filename != null) Files.writeString(Path.of(filename), new GsonBuilder().setPrettyPrinting().create().toJson(result) + "\n");
    return result;
  }
  private long incomplete() { return pending.values().stream().filter(frame -> frame.metrics() || frame.trace() != null && frame.trace().sampled()).count(); }
  private CompletableFuture<Boolean> shutdownSdk(long deadline, boolean flush) {
    if (!sdkStopping.compareAndSet(false, true)) return sdkStopped;
    // SDK entry points can perform synchronous collection or transport cleanup
    // before returning a result. Keep those operations off the application thread.
    Thread.ofPlatform().daemon().name("otelc-java-shutdown").start(() -> {
      boolean finished = false;
      try {
        if (flush) {
          var flushed = provider.forceFlush().join(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
          var stopped = provider.shutdown().join(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
          finished = flushed.isDone() && stopped.isDone();
        } else { exporter.shutdown(); provider.shutdown(); }
      } catch (Exception ignored) { /* The caller reports unfinished export. */ }
      finally {
        sdkStopped.complete(finished);
        if (!finished) exporter.shutdown();
      }
    });
    return sdkStopped;
  }
  @Override public void close() {
    if (!closed.compareAndSet(false, true)) return;
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(plan.integer("runtime", "shutdown_timeout_ms"));
    shutdownDeadline = deadline;
    try {
      if (control != null) control.close();
      if (!observations.tryLock(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS)) {
        discardCompletions = true; exportLoss.incrementAndGet(); shutdownSdk(deadline, false);
        return;
      }
      try { losses.get("incomplete").addAndGet(incomplete()); pending.clear(); current.remove(); if(tasks!=null) tasks.close(); if (traces != null) traces.shutdownPending(); }
      finally { observations.unlock(); }
      exportFinished = shutdownSdk(deadline, true).get(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
      if (!exportFinished) exportLoss.incrementAndGet();
    } catch (Exception ignored) {
      if (ignored instanceof InterruptedException) Thread.currentThread().interrupt();
      exportLoss.incrementAndGet(); shutdownSdk(deadline, false);
    }
    finally { try { report(); } catch (IOException ignored) { /* Reporting must not change application exit. */ } }
  }
}
