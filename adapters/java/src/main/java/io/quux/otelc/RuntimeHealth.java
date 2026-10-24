package io.quux.otelc;

import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdkBuilder;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizer;
import io.opentelemetry.sdk.common.CompletableResultCode;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

/** SDK API accounting, independent of meter enablement and downstream storage. */
final class RuntimeHealth {
  enum Signal { TRACES, METRICS }

  private static final String NOT_STARTED = "not_started";
  private static final String PENDING = "pending";
  private static final String SUCCEEDED = "succeeded";
  private static final String FAILED = "failed";
  private static final String TIMED_OUT = "timed_out";
  private static final String UNAVAILABLE = "unavailable";

  private static final class Exports {
    int observers;
    long batches;
    long succeededBatches;
    long failedBatches;
    long units;
    long succeededUnits;
    long failedUnits;

    long pending() { return batches - succeededBatches - failedBatches; }

    String json(String unit) {
      return "{\"exporters_observed\":" + observers
          + ",\"unit\":\"" + unit + "\",\"batches_started\":" + count(batches)
          + ",\"batches_succeeded\":" + count(succeededBatches)
          + ",\"batches_failed\":" + count(failedBatches)
          + ",\"batches_pending\":" + count(pending())
          + ",\"units_started\":" + count(units)
          + ",\"units_succeeded\":" + count(succeededUnits)
          + ",\"units_failed\":" + count(failedUnits) + "}";
    }

    private String count(long value) { return observers == 0 ? "null" : decimal(value); }
  }

  private final Path report;
  private final String policyHash;
  private final String selectionHash;
  private final long timeoutMillis;
  private final Exports traces = new Exports();
  private final Exports metrics = new Exports();
  private OpenTelemetrySdk sdk;
  private int processors;
  private int batchProcessors;
  private int closedProcessors;
  private long started;
  private long ended;
  private long metricFlushAttempts;
  private long metricFlushFailures;
  private String flushOutcome = NOT_STARTED;
  private String shutdownOutcome = NOT_STARTED;
  private String finalSnapshot;

  RuntimeHealth(Properties plan) {
    report = Path.of(plan.getProperty("health.report"));
    policyHash = digest(plan.getProperty("health.policy-sha256"));
    selectionHash = digest(plan.getProperty("health.selection-sha256"));
    timeoutMillis = Long.parseLong(plan.getProperty("health.shutdown-timeout-ms"));
    if (timeoutMillis < 1 || timeoutMillis > 1_000_000) {
      throw new IllegalArgumentException("Invalid runtime health shutdown bound");
    }
  }

  private static String digest(String value) {
    if (value == null || !value.matches("[0-9a-f]{64}")) {
      throw new IllegalArgumentException("Runtime health requires SHA-256 identities");
    }
    return value;
  }

  void install(AutoConfigurationCustomizer customizer) {
    if (!(customizer instanceof AutoConfiguredOpenTelemetrySdkBuilder builder)) {
      throw new IllegalStateException("Runtime health requires the supported SDK builder");
    }
    try {
      write(snapshot());
    } catch (IOException error) {
      throw new IllegalStateException("Cannot initialise private runtime health report", error);
    }
    // Register our replacement before removing the SDK's default JVM hook.
    Runtime.getRuntime().addShutdownHook(new Thread(this::finish, "otelc-sdk-health-shutdown"));
    builder.disableShutdownHook();
  }

  synchronized void attach(OpenTelemetrySdk value) {
    sdk = value;
    try {
      write(snapshot());
    } catch (IOException error) {
      throw new IllegalStateException("Cannot publish runtime health attachment", error);
    }
  }

  private Exports exports(Signal signal) { return signal == Signal.TRACES ? traces : metrics; }
  synchronized void exporterInstalled(Signal signal) { exports(signal).observers++; }
  synchronized void exportStarted(Signal signal, long units) {
    Exports value = exports(signal);
    value.batches++;
    value.units += units;
  }
  synchronized void exportFinished(Signal signal, long units, boolean success) {
    Exports value = exports(signal);
    if (success) {
      value.succeededBatches++;
      value.succeededUnits += units;
    } else {
      value.failedBatches++;
      value.failedUnits += units;
    }
    notifyAll();
  }
  synchronized void processorInstalled(boolean batch) {
    processors++;
    if (batch) batchProcessors++;
  }
  synchronized void processorClosed(boolean success) {
    if (success) closedProcessors++;
    notifyAll();
  }
  synchronized void spanStarted() { started++; }
  synchronized void spanEnded() { ended++; }

  private static String decimal(long value) { return "\"" + value + "\""; }

  synchronized String snapshot() {
    if (finalSnapshot != null) return finalSnapshot;
    return encode(false);
  }

  private synchronized boolean drained() {
    return SUCCEEDED.equals(flushOutcome)
        && SUCCEEDED.equals(shutdownOutcome) && processors == 1 && batchProcessors == 1
        && closedProcessors == 1 && traces.observers == 1 && traces.pending() == 0
        && started == ended && ended >= traces.units;
  }

  private String encode(boolean finished) {
    boolean drained = finished && drained();
    return "{\"schema_version\":1,\"kind\":\"otelc.runtime-health\",\"language\":\"java\""
        + ",\"pid\":" + ProcessHandle.current().pid()
        + ",\"policy_sha256\":\"" + policyHash + "\",\"selection_sha256\":\"" + selectionHash + "\""
        + ",\"scope\":\"sdk_pipeline\",\"phase\":\"" + (finished ? "final" : "running") + "\""
        + ",\"sdk_attached\":" + (sdk != null)
        + ",\"lifecycle\":{\"flush\":\"" + flushOutcome + "\",\"shutdown\":\"" + shutdownOutcome + "\"}"
        + ",\"metric_flush\":{\"attempts\":" + decimal(metricFlushAttempts)
        + ",\"failed_attempts\":" + decimal(metricFlushFailures) + "}"
        + ",\"span_accounting\":{\"processors_observed\":" + processors
        + ",\"batch_processors_observed\":" + batchProcessors
        + ",\"sampled_started\":" + (processors == 0 ? "null" : decimal(started))
        + ",\"sampled_ended\":" + (processors == 0 ? "null" : decimal(ended))
        + ",\"sampled_active\":" + (processors == 0 ? "null" : decimal(started - ended))
        + ",\"queue_pending\":" + (drained ? "\"0\"" : "null")
        + ",\"queue_dropped\":" + (drained ? decimal(ended - traces.units) : "null")
        + ",\"queue_evidence\":\"" + (drained ? "derived_after_shutdown" : UNAVAILABLE) + "\"}"
        + ",\"trace_exports\":" + traces.json("spans")
        + ",\"metric_exports\":" + metrics.json("metric_points")
        + ",\"downstream_receipt\":\"unavailable\",\"otlp_partial_rejection\":\"unavailable\"}\n";
  }

  private static String await(CompletableResultCode result, long deadline) {
    result.join(Math.max(0, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
    if (!result.isDone()) return TIMED_OUT;
    return result.isSuccess() ? SUCCEEDED : FAILED;
  }

  private static CompletableResultCode flushTraces(OpenTelemetrySdk value) {
    try {
      return value.getSdkTracerProvider().forceFlush();
    } catch (RuntimeException failure) {
      return CompletableResultCode.ofFailure();
    }
  }

  private String flushMetrics(OpenTelemetrySdk value, long deadline) throws InterruptedException {
    while (true) {
      synchronized (this) { metricFlushAttempts++; }
      String outcome;
      try { outcome = await(value.getSdkMeterProvider().forceFlush(), deadline); }
      catch (RuntimeException failure) {
        synchronized (this) { metricFlushFailures++; }
        return FAILED;
      }
      if (!FAILED.equals(outcome)) return outcome;
      synchronized (this) {
        metricFlushFailures++;
        long remaining = deadline - System.nanoTime();
        if (remaining <= 0) return FAILED;
        // PeriodicMetricReader rejects concurrent flushes. A retry must make a
        // real SDK call; preserve every failed attempt instead of relabelling it.
        TimeUnit.NANOSECONDS.timedWait(this, Math.min(remaining, TimeUnit.MILLISECONDS.toNanos(10)));
      }
    }
  }

  private String flush(OpenTelemetrySdk value, long deadline) throws InterruptedException {
    CompletableResultCode tracesResult = flushTraces(value);
    String metricsOutcome = flushMetrics(value, deadline);
    String tracesOutcome = await(tracesResult, deadline);
    if (TIMED_OUT.equals(tracesOutcome) || TIMED_OUT.equals(metricsOutcome)) return TIMED_OUT;
    return FAILED.equals(tracesOutcome) || FAILED.equals(metricsOutcome) ? FAILED : SUCCEEDED;
  }

  private static String shutdown(OpenTelemetrySdk value, long deadline) {
    try {
      return await(CompletableResultCode.ofAll(List.of(value.getSdkTracerProvider().shutdown(),
          value.getSdkMeterProvider().shutdown(), value.getSdkLoggerProvider().shutdown())), deadline);
    } catch (RuntimeException failure) {
      return FAILED;
    }
  }

  private void drain(OpenTelemetrySdk value, long deadline) {
    try {
      synchronized (this) { flushOutcome = PENDING; }
      String flushed = flush(value, deadline);
      synchronized (this) { flushOutcome = flushed; shutdownOutcome = PENDING; }
      String closed = shutdown(value, deadline);
      synchronized (this) {
        shutdownOutcome = closed;
        // Completion of an SDK result can precede execution of our own result callback.
        while ((traces.pending() != 0 || metrics.pending() != 0 || closedProcessors < processors)
            && System.nanoTime() < deadline) {
          TimeUnit.NANOSECONDS.timedWait(this, Math.max(1, deadline - System.nanoTime()));
        }
      }
    } catch (InterruptedException interrupted) {
      Thread.currentThread().interrupt();
    }
  }

  void finish() {
    OpenTelemetrySdk value;
    synchronized (this) {
      if (finalSnapshot != null) return;
      value = sdk;
    }
    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMillis);
    if (value != null) {
      // Also bound providers/exporters that block before returning their async result.
      Thread drain = Thread.ofPlatform().daemon().name("otelc-sdk-drain").start(() -> drain(value, deadline));
      try {
        TimeUnit.NANOSECONDS.timedJoin(drain, Math.max(1, deadline - System.nanoTime()));
      } catch (InterruptedException interrupted) {
        Thread.currentThread().interrupt();
      }
    }
    String result;
    synchronized (this) {
      if (value == null) { flushOutcome = UNAVAILABLE; shutdownOutcome = UNAVAILABLE; }
      else {
        if (PENDING.equals(flushOutcome) || NOT_STARTED.equals(flushOutcome)) flushOutcome = TIMED_OUT;
        if (PENDING.equals(shutdownOutcome) || NOT_STARTED.equals(shutdownOutcome)) shutdownOutcome = TIMED_OUT;
      }
      finalSnapshot = encode(true);
      result = finalSnapshot;
    }
    try { write(result); }
    catch (IOException ignored) {
      // Leave the non-final report in place. A consumer must reject missing final evidence.
    }
    var witness = io.quux.otelc.witness.WitnessRuntime.get();
    if (witness != null) witness.finish(drained());
  }

  private void write(String value) throws IOException {
    if (!Files.getPosixFilePermissions(report.getParent()).equals(PosixFilePermissions.fromString("rwx------"))) {
      throw new IOException("runtime health directory must be private (0700)");
    }
    Path temporary = Files.createTempFile(report.getParent(), ".health-", ".json",
        PosixFilePermissions.asFileAttribute(PosixFilePermissions.fromString("rw-------")));
    try {
      Files.writeString(temporary, value);
      Files.move(temporary, report, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    } finally {
      Files.deleteIfExists(temporary);
    }
  }
}
