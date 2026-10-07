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
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/** Bounded pending observations and language-native SDK metrics. */
public final class Telemetry implements AutoCloseable {
  final Plan plan;
  final long timeoutMs;
  final AtomicLong exportLoss = new AtomicLong();
  final Map<String, AtomicLong> losses = new LinkedHashMap<>();
  final ConcurrentHashMap<String, Function> functions = new ConcurrentHashMap<>();
  final ConcurrentHashMap<Long, Frame> pending = new ConcurrentHashMap<>();
  private final AtomicLong next = new AtomicLong();
  private final AtomicBoolean closed = new AtomicBoolean();
  volatile boolean enabled;
  private volatile boolean exportFinished;
  private final Exporter exporter;
  private final SdkMeterProvider provider;
  private final LongCounter calls;
  private final LongCounter unwinds;
  private final DoubleHistogram duration;
  private Control control;
  record Frame(String name, long started) {}
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
    exporter = new Exporter(this);
    var reader = PeriodicMetricReader.builder(exporter).setInterval(Duration.ofMillis(plan.integer("export", "interval_ms"))).build();
    var attributes = Attributes.builder().put("service.name", plan.string("resource", "service_name")).put("service.version", plan.string("resource", "service_version")).put("service.instance.id", Long.toString(ProcessHandle.current().pid()));
    plan.section("resource").getAsJsonObject("attributes").entrySet().forEach(entry -> attributes.put(entry.getKey(), entry.getValue().getAsString()));
    var boundaries = plan.section("metrics").getAsJsonArray("histogram_boundaries_seconds").asList().stream().map(value -> value.getAsDouble()).toList();
    var builder = SdkMeterProvider.builder().setResource(Resource.create(attributes.build())).registerMetricReader(reader);
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
  }
  synchronized boolean register(String name) {
    if (functions.containsKey(name)) return true;
    if (functions.size() >= plan.integer("runtime", "max_functions") || name.getBytes(StandardCharsets.UTF_8).length > 1024) { lose("function_capacity"); return false; }
    functions.put(name, new Function(name)); return true;
  }
  synchronized long enter(String name) {
    if (!enabled || closed.get()) return 0;
    if (!register(name)) return 0;
    if (pending.size() >= plan.integer("runtime", "max_active_calls")) { lose("active_call_capacity"); return 0; }
    long token = next.incrementAndGet();
    if (token <= 0) { lose("invalid"); return 0; }
    pending.put(token, new Frame(name, System.nanoTime())); return token;
  }
  void exit(long token, boolean escaped) {
    if (token == 0) return;
    long ended = System.nanoTime();
    var frame = pending.remove(token); if (frame == null) return;
    var function = functions.get(frame.name()); function.calls.incrementAndGet();
    if (escaped) function.unwinds.incrementAndGet();
    calls.add(1, function.attributes); duration.record((ended - frame.started()) / 1e9, function.attributes);
    if (escaped) unwinds.add(1, function.attributes);
  }
  void lose(String reason) { losses.get(reason).incrementAndGet(); }
  void bindControl() throws IOException { if (plan.controlSocket() != null) control = new Control(this); }
  long count() { return functions.values().stream().mapToLong(value -> value.calls.get()).sum(); }
  Map<String, Object> report() throws IOException {
    var result = new LinkedHashMap<String, Object>(); result.put("schema_version", 1); result.put("language", "java"); result.put("pid", ProcessHandle.current().pid()); result.put("export_finished", exportFinished); result.put("function_calls", count());
    var values = new LinkedHashMap<String, Object>(); functions.forEach((name, value) -> values.put(name, Map.of("count", value.calls.get(), "unwinds", value.unwinds.get()))); result.put("functions", values);
    var lost = new LinkedHashMap<String, Long>(); losses.forEach((name, value) -> lost.put(name, value.get())); lost.compute("incomplete", (name, value) -> value + pending.size()); result.put("losses", lost); result.put("export_loss", exportLoss.get());
    String filename = System.getenv("OTELC_REPORT_PATH"); if (filename != null) Files.writeString(Path.of(filename), new GsonBuilder().setPrettyPrinting().create().toJson(result) + "\n");
    return result;
  }
  @Override public void close() {
    if (!closed.compareAndSet(false, true)) return;
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(plan.integer("runtime", "shutdown_timeout_ms"));
    losses.get("incomplete").addAndGet(pending.size()); pending.clear();
    try {
      if (control != null) control.close();
      var flushed = provider.forceFlush().join(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
      var stopped = provider.shutdown().join(Math.max(1, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
      exportFinished = flushed.isDone() && stopped.isDone();
      if (!exportFinished) { exportLoss.incrementAndGet(); exporter.shutdown(); }
    } catch (Exception ignored) { exportLoss.incrementAndGet(); exporter.shutdown(); }
    try { report(); } catch (IOException ignored) { /* Reporting must not change application exit. */ }
  }
}
