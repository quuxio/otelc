package io.quux.otelc;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizer;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.metrics.InstrumentType;
import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.data.AggregationTemporality;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.metrics.export.MetricExporter;
import io.opentelemetry.sdk.metrics.export.PeriodicMetricReader;
import io.opentelemetry.sdk.testing.exporter.InMemoryMetricReader;
import io.opentelemetry.sdk.testing.exporter.InMemorySpanExporter;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Duration;
import java.util.Collection;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.io.TempDir;

class RuntimeHealthTests {
  @TempDir Path directory;

  @BeforeAll static void initialiseOutputCapture() {
    // Surefire 3.6.0 initialises StackWalkerStrategy from the first write. SDK daemon
    // threads deliberately have a null context loader; prime capture on the test thread.
    System.err.println("Runtime health fixtures exercise intentional export failures and queue overflow.");
  }

  private Properties plan(long timeout) throws Exception {
    Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwx------"));
    Properties plan = new Properties();
    plan.setProperty("health.report", directory.resolve("health.json").toString());
    plan.setProperty("health.policy-sha256", "a".repeat(64));
    plan.setProperty("health.selection-sha256", "b".repeat(64));
    plan.setProperty("health.shutdown-timeout-ms", Long.toString(timeout));
    return plan;
  }

  private static long count(String json, String name) {
    var match = Pattern.compile("\"" + name + "\":\"([0-9]+)\"").matcher(json);
    assertTrue(match.find(), () -> "missing explicit counter " + name + " in " + json);
    return Long.parseLong(match.group(1));
  }

  private static class Exporter implements SpanExporter {
    CompletableResultCode result = CompletableResultCode.ofSuccess();
    boolean throwing;
    final CountDownLatch called = new CountDownLatch(1);
    public CompletableResultCode export(Collection<SpanData> spans) {
      called.countDown();
      if (throwing) throw new IllegalStateException("fixture export failure");
      return result;
    }
    public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
    public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
  }

  @Test void identitiesAndUnavailableObserversFailClosed() throws Exception {
    Properties plan = plan(1000);
    RuntimeHealth health = new RuntimeHealth(plan);
    assertTrue(health.snapshot().contains("\"units_started\":null"));
    assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
    health.finish();
    String json = Files.readString(directory.resolve("health.json"));
    assertTrue(json.contains("\"phase\":\"final\""));
    assertTrue(json.contains("\"sdk_attached\":false"));
    assertTrue(json.contains("\"flush\":\"unavailable\""));
    assertTrue(json.contains("\"policy_sha256\":\"" + "a".repeat(64) + "\""));
    assertTrue(json.contains("\"pid\":" + ProcessHandle.current().pid()));
    assertEquals(PosixFilePermissions.fromString("rw-------"),
        Files.getPosixFilePermissions(directory.resolve("health.json")));
    plan.remove("health.selection-sha256");
    assertThrows(IllegalArgumentException.class, () -> new RuntimeHealth(plan));
    plan.setProperty("health.selection-sha256", "b".repeat(64));
    plan.setProperty("health.shutdown-timeout-ms", "0");
    assertThrows(IllegalArgumentException.class, () -> new RuntimeHealth(plan));
  }

  @Test void installationUsesSupportedBuilderAndPrivateAtomicReport() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    AutoConfigurationCustomizer proxy = (AutoConfigurationCustomizer) java.lang.reflect.Proxy.newProxyInstance(
        getClass().getClassLoader(), new Class<?>[] {AutoConfigurationCustomizer.class},
        (instance, method, arguments) -> instance);
    assertThrows(IllegalStateException.class, () -> health.install(proxy));
    var builder = AutoConfiguredOpenTelemetrySdk.builder();
    health.install(builder);
    assertEquals(health.snapshot(), Files.readString(directory.resolve("health.json")));
    Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwxr-xr-x"));
    assertThrows(IllegalStateException.class, () -> health.install(builder));
    try (var sdk = OpenTelemetrySdk.builder().build()) {
      assertThrows(IllegalStateException.class, () -> health.attach(sdk));
    }
    String old = Files.readString(directory.resolve("health.json"));
    health.finish();
    assertEquals(old, Files.readString(directory.resolve("health.json")),
        "a failed final write must leave a non-final report for consumers to reject");
  }

  @Test void spanExporterPreservesAsyncFailureAndThrownException() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    Exporter delegate = new Exporter();
    delegate.result = new CompletableResultCode();
    SpanExporter exporter = ExportHealth.spans(delegate, health);
    assertSame(delegate.result, exporter.export(List.of()));
    assertEquals(1, count(health.snapshot(), "batches_pending"));
    delegate.result.fail();
    assertEquals(1, count(health.snapshot(), "batches_failed"));
    delegate.throwing = true;
    assertThrows(IllegalStateException.class, () -> exporter.export(List.of()));
    assertEquals(2, count(health.snapshot(), "batches_failed"));
    assertEquals(0, count(health.snapshot(), "batches_pending"));
    assertTrue(exporter.flush().isSuccess());
    assertTrue(exporter.shutdown().isSuccess());
  }

  @Test void metricUnitsAreActualPointsAndEmissionOffDoesNotInventAttempts() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    MetricExporter delegate = new MetricExporter() {
      public CompletableResultCode export(Collection<MetricData> values) { return CompletableResultCode.ofFailure(); }
      public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
      public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
      public AggregationTemporality getAggregationTemporality(InstrumentType type) { return AggregationTemporality.CUMULATIVE; }
    };
    MetricExporter exporter = ExportHealth.metrics(delegate, health);
    var gateState = new EmissionGate.State(false);
    var gate = new EmissionGate(exporter, gateState);
    var reader = InMemoryMetricReader.create();
    try (var provider = SdkMeterProvider.builder().registerMetricReader(reader).build()) {
      provider.get("fixture").counterBuilder("counter").build().add(3);
      provider.get("fixture").histogramBuilder("histogram").build().record(4);
      var points = reader.collectAllMetrics();
      assertTrue(gate.export(points).isSuccess());
      assertEquals(0, count(health.snapshot(), "units_started"));
      gateState.set(true);
      assertFalse(gate.export(points).isSuccess());
      assertEquals(2, count(health.snapshot(), "units_failed"));
    }
    assertEquals(delegate.getMemoryMode(), exporter.getMemoryMode());
    assertEquals(delegate.getDefaultAggregation(InstrumentType.HISTOGRAM), exporter.getDefaultAggregation(InstrumentType.HISTOGRAM));
    assertEquals(AggregationTemporality.CUMULATIVE, exporter.getAggregationTemporality(InstrumentType.COUNTER));
    assertTrue(exporter.flush().isSuccess());
    assertTrue(exporter.shutdown().isSuccess());
  }

  @Test void cleanDrainDerivesExplicitZeroAndKeepsExporterFailuresSeparate() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    Exporter delegate = new Exporter();
    delegate.result = CompletableResultCode.ofFailure();
    var processor = new HealthSpanProcessor(BatchSpanProcessor.builder(ExportHealth.spans(delegate, health))
        .setScheduleDelay(Duration.ofHours(1)).build(), health);
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(processor).build()).build();
    health.attach(sdk);
    sdk.getTracer("test").spanBuilder("failed_export").startSpan().end();
    assertTrue(health.snapshot().contains("\"queue_pending\":null"));
    health.finish();
    String json = health.snapshot();
    assertTrue(json.contains("\"shutdown\":\"succeeded\""));
    assertEquals(1, count(json, "sampled_ended"));
    assertEquals(1, count(json, "units_failed"));
    assertEquals(0, count(json, "queue_pending"));
    assertEquals(0, count(json, "queue_dropped"));
    assertTrue(json.contains("\"downstream_receipt\":\"unavailable\""));
    health.spanStarted();
    health.finish();
    assertEquals(json, health.snapshot(), "final evidence is a frozen shutdown checkpoint");
  }

  @Test void realBoundedQueueReportsLossAfterBlockedExportDrains() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(2000));
    Exporter delegate = new Exporter();
    delegate.result = new CompletableResultCode();
    var processor = new HealthSpanProcessor(BatchSpanProcessor.builder(ExportHealth.spans(delegate, health))
        .setMeterProvider(io.opentelemetry.api.metrics.MeterProvider.noop())
        .setMaxQueueSize(2).setMaxExportBatchSize(1).setScheduleDelay(Duration.ofHours(1)).build(), health);
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder().addSpanProcessor(processor).build()).build();
    health.attach(sdk);
    var tracer = sdk.getTracer("queue-test");
    tracer.spanBuilder("first").startSpan().end();
    assertTrue(delegate.called.await(1, TimeUnit.SECONDS));
    for (int i = 0; i < 20; i++) tracer.spanBuilder("overflow").startSpan().end();
    assertEquals(21, count(health.snapshot(), "sampled_ended"));
    assertEquals(1, count(health.snapshot(), "batches_pending"));
    assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
    delegate.result.succeed();
    health.finish();
    String json = health.snapshot();
    assertEquals(0, count(json, "queue_pending"));
    assertTrue(count(json, "queue_dropped") > 0, json);
    assertEquals(21, count(json, "queue_dropped") + count(json, "units_started"));
  }

  @Test void unsampledAndOpenSpansDoNotCreateFalseQueueProof() throws Exception {
    for (Sampler sampler : List.of(Sampler.alwaysOff(), Sampler.alwaysOn())) {
      RuntimeHealth health = new RuntimeHealth(plan(1000));
      var exporter = ExportHealth.spans(InMemorySpanExporter.create(), health);
      var provider = SdkTracerProvider.builder().setSampler(sampler)
          .addSpanProcessor(new HealthSpanProcessor(BatchSpanProcessor.create(exporter), health)).build();
      var sdk = OpenTelemetrySdk.builder().setTracerProvider(provider).build();
      health.attach(sdk);
      var span = sdk.getTracer("open-test").spanBuilder("open").startSpan();
      health.finish();
      if (span.getSpanContext().isSampled()) {
        assertEquals(1, count(health.snapshot(), "sampled_active"));
        assertTrue(health.snapshot().contains("\"queue_pending\":null"));
      } else {
        assertEquals(0, count(health.snapshot(), "sampled_started"));
        assertEquals(0, count(health.snapshot(), "queue_dropped"));
      }
      span.end();
    }
  }

  private static class Processor implements SpanProcessor {
    int starts;
    int ends;
    CompletableResultCode flushed = CompletableResultCode.ofSuccess();
    CompletableResultCode closed = CompletableResultCode.ofSuccess();
    public void onStart(Context parent, ReadWriteSpan span) { starts++; }
    public boolean isStartRequired() { return true; }
    public void onEnd(ReadableSpan span) { ends++; }
    public boolean isEndRequired() { return true; }
    public CompletableResultCode forceFlush() { return flushed; }
    public CompletableResultCode shutdown() { return closed; }
  }

  @Test void unsupportedProcessorAndShutdownFailureStayUnavailable() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    Processor delegate = new Processor();
    delegate.closed = CompletableResultCode.ofFailure();
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder()
        .addSpanProcessor(new HealthSpanProcessor(delegate, health)).build()).build();
    health.attach(sdk);
    sdk.getTracer("other-processor").spanBuilder("test").startSpan().end();
    health.finish();
    assertEquals(1, delegate.starts);
    assertEquals(1, delegate.ends);
    assertTrue(health.snapshot().contains("\"shutdown\":\"failed\""));
    assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
  }

  @Test void neverCompletingFlushHasABoundedFinalReport() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(40));
    Processor delegate = new Processor();
    delegate.flushed = new CompletableResultCode();
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder()
        .addSpanProcessor(new HealthSpanProcessor(delegate, health)).build()).build();
    health.attach(sdk);
    long before = System.nanoTime();
    health.finish();
    assertTrue(System.nanoTime() - before < TimeUnit.SECONDS.toNanos(1));
    assertTrue(health.snapshot().contains("\"flush\":\"timed_out\""));
    assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
    delegate.flushed.succeed();
  }

  @Test void blockedShutdownCallIsBoundedBeforeItReturnsAResult() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(40));
    CountDownLatch entered = new CountDownLatch(1);
    CountDownLatch release = new CountDownLatch(1);
    Processor delegate = new Processor() {
      @Override public CompletableResultCode shutdown() {
        entered.countDown();
        try { release.await(); }
        catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); }
        return CompletableResultCode.ofSuccess();
      }
    };
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder()
        .addSpanProcessor(new HealthSpanProcessor(delegate, health)).build()).build();
    health.attach(sdk);
    try {
      long before = System.nanoTime();
      health.finish();
      assertTrue(System.nanoTime() - before < TimeUnit.SECONDS.toNanos(1));
      assertTrue(entered.await(1, TimeUnit.SECONDS));
      assertTrue(health.snapshot().contains("\"shutdown\":\"timed_out\""));
      assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
    } finally { release.countDown(); }
  }

  @Test void synchronousProviderFailureIsNotReportedAsCleanShutdown() throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(1000));
    Processor delegate = new Processor() {
      @Override public CompletableResultCode forceFlush() { throw new IllegalStateException("fixture flush failure"); }
    };
    var sdk = OpenTelemetrySdk.builder().setTracerProvider(SdkTracerProvider.builder()
        .addSpanProcessor(new HealthSpanProcessor(delegate, health)).build()).build();
    health.attach(sdk);
    health.finish();
    assertTrue(health.snapshot().contains("\"flush\":\"failed\""));
    assertTrue(health.snapshot().contains("\"shutdown\":\"succeeded\""));
    assertTrue(health.snapshot().contains("\"queue_dropped\":null"));
    sdk.close();
  }

  @Test void shutdownRetriesFlushRejectedByAnExistingMetricExport() throws Exception {
    busyMetricFlush(true);
    busyMetricFlush(false);
  }

  private void busyMetricFlush(boolean exportSuccess) throws Exception {
    RuntimeHealth health = new RuntimeHealth(plan(2000));
    CompletableResultCode pending = new CompletableResultCode();
    CountDownLatch busyFlush = new CountDownLatch(1);
    MetricExporter delegate = new MetricExporter() {
      public CompletableResultCode export(Collection<MetricData> values) { return pending; }
      public CompletableResultCode flush() { busyFlush.countDown(); return CompletableResultCode.ofSuccess(); }
      public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
      public AggregationTemporality getAggregationTemporality(InstrumentType type) { return AggregationTemporality.CUMULATIVE; }
    };
    var reader = PeriodicMetricReader.builder(ExportHealth.metrics(delegate, health))
        .setInterval(Duration.ofHours(1)).build();
    var sdk = OpenTelemetrySdk.builder().setMeterProvider(SdkMeterProvider.builder()
        .registerMetricReader(reader).build()).build();
    health.attach(sdk);
    sdk.getMeter("busy").counterBuilder("calls").build().add(1);
    CompletableResultCode first = sdk.getSdkMeterProvider().forceFlush();
    assertFalse(first.isDone());
    Thread finisher = Thread.ofPlatform().start(health::finish);
    try {
      assertTrue(busyFlush.await(1, TimeUnit.SECONDS), "the concurrent SDK flush must actually be attempted");
    } finally {
      if (exportSuccess) pending.succeed(); else pending.fail();
    }
    finisher.join(3000);
    assertFalse(finisher.isAlive());
    assertTrue(health.snapshot().contains("\"flush\":\"succeeded\""), health.snapshot());
    assertTrue(health.snapshot().contains("\"shutdown\":\"succeeded\""), health.snapshot());
    assertTrue(count(health.snapshot(), "attempts") >= 2);
    assertTrue(count(health.snapshot(), "failed_attempts") >= 1);
    assertEquals(exportSuccess, count(health.snapshot(), "units_failed") == 0,
        "a successful retry cannot erase failed export units");
  }
}
