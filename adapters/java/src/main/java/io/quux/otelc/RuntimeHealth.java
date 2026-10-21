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
  private String flushOutcome = "not_started";
  private String shutdownOutcome = "not_started";
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

  private String encode(boolean finished) {
    boolean drained = finished && "succeeded".equals(flushOutcome)
        && "succeeded".equals(shutdownOutcome) && processors == 1 && batchProcessors == 1
        && closedProcessors == 1 && traces.observers == 1 && traces.pending() == 0
        && started == ended && ended >= traces.units;
    return "{\"schema_version\":1,\"kind\":\"otelc.runtime-health\",\"language\":\"java\""
        + ",\"pid\":" + ProcessHandle.current().pid()
        + ",\"policy_sha256\":\"" + policyHash + "\",\"selection_sha256\":\"" + selectionHash + "\""
        + ",\"scope\":\"sdk_pipeline\",\"phase\":\"" + (finished ? "final" : "running") + "\""
        + ",\"sdk_attached\":" + (sdk != null)
        + ",\"lifecycle\":{\"flush\":\"" + flushOutcome + "\",\"shutdown\":\"" + shutdownOutcome + "\"}"
        + ",\"span_accounting\":{\"processors_observed\":" + processors
        + ",\"batch_processors_observed\":" + batchProcessors
        + ",\"sampled_started\":" + (processors == 0 ? "null" : decimal(started))
        + ",\"sampled_ended\":" + (processors == 0 ? "null" : decimal(ended))
        + ",\"sampled_active\":" + (processors == 0 ? "null" : decimal(started - ended))
        + ",\"queue_pending\":" + (drained ? "\"0\"" : "null")
        + ",\"queue_dropped\":" + (drained ? decimal(ended - traces.units) : "null")
        + ",\"queue_evidence\":\"" + (drained ? "derived_after_shutdown" : "unavailable") + "\"}"
        + ",\"trace_exports\":" + traces.json("spans")
        + ",\"metric_exports\":" + metrics.json("metric_points")
        + ",\"downstream_receipt\":\"unavailable\",\"otlp_partial_rejection\":\"unavailable\"}\n";
  }

  private static String await(CompletableResultCode result, long deadline) {
    result.join(Math.max(0, deadline - System.nanoTime()), TimeUnit.NANOSECONDS);
    return !result.isDone() ? "timed_out" : result.isSuccess() ? "succeeded" : "failed";
  }

  private static String flush(OpenTelemetrySdk value, long deadline) {
    try {
      return await(CompletableResultCode.ofAll(List.of(
          value.getSdkTracerProvider().forceFlush(), value.getSdkMeterProvider().forceFlush())), deadline);
    } catch (RuntimeException failure) {
      return "failed";
    }
  }

  private static String shutdown(OpenTelemetrySdk value, long deadline) {
    try {
      return await(CompletableResultCode.ofAll(List.of(value.getSdkTracerProvider().shutdown(),
          value.getSdkMeterProvider().shutdown(), value.getSdkLoggerProvider().shutdown())), deadline);
    } catch (RuntimeException failure) {
      return "failed";
    }
  }

  private void drain(OpenTelemetrySdk value, long deadline) {
    try {
      synchronized (this) { flushOutcome = "pending"; }
      String flushed = flush(value, deadline);
      synchronized (this) { flushOutcome = flushed; shutdownOutcome = "pending"; }
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
      if (value == null) { flushOutcome = "unavailable"; shutdownOutcome = "unavailable"; }
      else {
        if ("pending".equals(flushOutcome) || "not_started".equals(flushOutcome)) flushOutcome = "timed_out";
        if ("pending".equals(shutdownOutcome) || "not_started".equals(shutdownOutcome)) shutdownOutcome = "timed_out";
      }
      finalSnapshot = encode(true);
      result = finalSnapshot;
    }
    try { write(result); }
    catch (IOException ignored) {
      // Leave the non-final report in place. A consumer must reject missing final evidence.
    }
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
