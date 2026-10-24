package io.quux.otelc;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.autoconfigure.AutoConfiguredOpenTelemetrySdk;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.metrics.InstrumentType;
import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.data.AggregationTemporality;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.metrics.data.LongPointData;
import io.opentelemetry.sdk.metrics.export.MetricExporter;
import io.opentelemetry.sdk.testing.exporter.InMemoryMetricReader;
import io.quux.otelc.lifecycle.Observer;
import java.lang.ref.ReferenceQueue;
import java.lang.ref.WeakReference;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

class ExtensionTests {
  static final class TestResource {
    @Override public int hashCode() { throw new AssertionError("application hashCode called"); }
    @Override public boolean equals(Object other) { throw new AssertionError("application equals called"); }
  }
  private static final InMemoryMetricReader GLOBAL = InMemoryMetricReader.create();
  private static Path directory;

  @BeforeAll static void configure() throws Exception {
    directory = Files.createTempDirectory(Path.of("/tmp"), "otelc-junit-");
    Files.setPosixFilePermissions(directory, PosixFilePermissions.fromString("rwx------"));
    Path plan = directory.resolve("plan.properties");
    Files.writeString(plan, "control.socket=" + directory.resolve("agent.sock") + "\nmetrics.buckets=0.001,0.1\n");
    System.setProperty("otelc.plan", plan.toString());
    System.setProperty("otelc.lifetime.max-live", "2");
    System.setProperty("otelc.lifetime.resources", TestResource.class.getName());
    GlobalOpenTelemetry.resetForTest();
    OpenTelemetrySdk.builder().setMeterProvider(SdkMeterProvider.builder().registerMetricReader(GLOBAL).build()).buildAndRegisterGlobal();
  }

  static long count(String name) {
    return count(name, null);
  }

  static long count(String name, String scope) {
    return GLOBAL.collectAllMetrics().stream().filter(metric -> metric.getName().equals(name))
        .filter(metric -> scope == null || scope.equals(metric.getInstrumentationScopeInfo().getName()))
        .flatMap(metric -> metric.getLongSumData().getPoints().stream()).mapToLong(LongPointData::getValue).sum();
  }

  @Test void cachedAndNewInstrumentsFollowCollectionState() {
    InMemoryMetricReader reader = InMemoryMetricReader.create();
    try (SdkMeterProvider provider = SdkMeterProvider.builder().registerMetricReader(reader).build()) {
      LiveState state = new LiveState(new Properties());
      state.attach(provider);
      var counter = provider.get("cached").counterBuilder("cached").build();
      counter.add(3);
      state.command("collection off");
      counter.add(100);
      var late = provider.get("late").counterBuilder("late").build();
      late.add(200);
      assertTrue(reader.collectAllMetrics().isEmpty(), "disabled meters suppress existing metric streams");
      assertFalse(state.collecting());
      state.command("collection on");
      counter.add(7);
      late.add(5);
      assertEquals(15, reader.collectAllMetrics().stream().flatMap(metric -> metric.getLongSumData().getPoints().stream()).mapToLong(LongPointData::getValue).sum());
      for (String command : List.of("off", "on", "emission off", "emission on", "flush", "status")) {
        assertTrue(state.command(command).contains("\"schema_version\":1"));
      }
      assertTrue(state.command("unknown").contains("error"));
    }
  }

  @Test void exporterGateTracksAsynchronousCompletionAndFailure() {
    class Delegate implements MetricExporter {
      int calls;
      boolean fail;
      CompletableResultCode pending = new CompletableResultCode();
      public CompletableResultCode export(Collection<MetricData> values) {
        calls++;
        if (fail) throw new IllegalStateException("export failed");
        return pending;
      }
      public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
      public CompletableResultCode shutdown() { return CompletableResultCode.ofSuccess(); }
      public AggregationTemporality getAggregationTemporality(InstrumentType type) { return AggregationTemporality.CUMULATIVE; }
    }
    Delegate delegate = new Delegate();
    EmissionGate.State state = new EmissionGate.State(false);
    EmissionGate gate = new EmissionGate(delegate, state);
    assertTrue(gate.export(List.of()).isSuccess());
    assertTrue(gate.flush().isSuccess());
    assertEquals(0, delegate.calls);
    state.set(true);
    gate.export(List.of());
    assertEquals(1, state.inFlight());
    state.set(false);
    assertEquals(1, state.inFlight());
    delegate.pending.succeed();
    assertEquals(0, state.inFlight());
    state.set(true);
    delegate.fail = true;
    assertThrows(IllegalStateException.class, () -> gate.export(List.of()));
    assertEquals(0, state.inFlight());
    assertTrue(gate.flush().isSuccess());
    assertTrue(gate.shutdown().isSuccess());
    assertEquals(AggregationTemporality.CUMULATIVE, gate.getAggregationTemporality(InstrumentType.COUNTER));
    assertNotNull(gate.getDefaultAggregation(InstrumentType.HISTOGRAM));
  }

  @Test void directMethodMetricsDoNotDependOnSpans() {
    LiveState state = new LiveState(new Properties());
    var listener = MethodMetrics.listener(GlobalOpenTelemetry.getMeter("test-methods"), state);
    Attributes attrs = Attributes.of(AttributeKey.stringKey("code.function.name"), "workload");
    Context admitted = listener.onStart(Context.root(), attrs, 1000);
    listener.onEnd(admitted, Attributes.empty(), 2000);
    listener.onEnd(listener.onStart(Context.root(), Attributes.empty(), 1000), Attributes.empty(), 1500);
    Attributes old = Attributes.of(AttributeKey.stringKey("code.function"), "legacy");
    listener.onEnd(listener.onStart(Context.root(), old, 1000), Attributes.empty(), 1500);
    state.updateCollection(false);
    listener.onEnd(listener.onStart(Context.root(), attrs, 1000), Attributes.empty(), 2000);
    listener.onEnd(listener.onStart(admitted, attrs, 1500), Attributes.empty(), 2000);
    assertEquals(3, state.completed.get());
    assertEquals(3, count("otelc.function.calls", "test-methods"));
  }

  @Test void safeMethodObserverPreservesValuesAndClosesSyncAndAsyncScopes() {
    assertEquals("unchanged", io.quux.otelc.methods.Observer.exit(null, "()Ljava/lang/String;", "unchanged", null));
    var call = io.quux.otelc.methods.Observer.enter(ExtensionTests.class, "run");
    assertNotNull(call);
    assertEquals(3, io.quux.otelc.methods.Observer.exit(call, "()I", 3, null));
    var errorCall = io.quux.otelc.methods.Observer.enter(ExtensionTests.class, "fail");
    assertNull(io.quux.otelc.methods.Observer.exit(errorCall, "()V", null, new IllegalArgumentException()));
    var primitiveError = io.quux.otelc.methods.Observer.enter(ExtensionTests.class, "primitiveFail");
    assertEquals(0, io.quux.otelc.methods.Observer.exit(primitiveError, "()I", 0, new IllegalStateException()));
    Context previous = Context.current();
    var absent = io.quux.otelc.methods.Observer.enter(ExtensionTests.class, "absent");
    assertNull(io.quux.otelc.methods.Observer.exit(absent, "()Lunavailable/Optional;", null, null));
    assertSame(previous, Context.current());
    var future = new java.util.concurrent.CompletableFuture<Integer>();
    var async = io.quux.otelc.methods.Observer.enter(ExtensionTests.class, "async");
    assertSame(future, io.quux.otelc.methods.Observer.exit(async, "()Ljava/util/concurrent/CompletableFuture;", future, null));
    future.complete(7);
    assertEquals(7, future.join());
  }

  @Test void customizerAppliesOnlyToTheOfficialMethodInstrumenter() {
    var factories = new java.util.ArrayList<io.opentelemetry.instrumentation.api.instrumenter.OperationMetrics>();
    for (String name : List.of("another-instrumentation", "io.opentelemetry.methods")) {
      var customizer = (io.opentelemetry.instrumentation.api.incubator.instrumenter.InstrumenterCustomizer)
          java.lang.reflect.Proxy.newProxyInstance(getClass().getClassLoader(),
              new Class<?>[] {io.opentelemetry.instrumentation.api.incubator.instrumenter.InstrumenterCustomizer.class},
              (proxy, method, arguments) -> {
                if (method.getName().equals("getInstrumentationName")) return name;
                if (method.getName().equals("addOperationMetrics")) {
                  factories.add((io.opentelemetry.instrumentation.api.instrumenter.OperationMetrics) arguments[0]);
                  return proxy;
                }
                throw new UnsupportedOperationException(method.getName());
              });
      new MethodMetrics().customize(customizer);
    }
    assertEquals(1, factories.size());
    assertNotNull(factories.getFirst().create(GlobalOpenTelemetry.getMeter("customized-methods")));
  }

  @Test void lifetimeModuleRestrictsTypesAndSeparatesConstructorFromReleaseAdvice() throws Exception {
    System.clearProperty("otelc.lifetime.types");
    LifetimeModule module = new LifetimeModule();
    assertTrue(module.typeInstrumentations().isEmpty());
    for (String helper : module.getAdditionalHelperClassNames()) {
      assertNotNull(Class.forName(helper, false, getClass().getClassLoader()));
    }
    System.setProperty("otelc.lifetime.types", "examples.SelectedWorkItem");
    try {
      var instrumentation = module.typeInstrumentations().getFirst();
      var selected = new net.bytebuddy.description.type.TypeDescription.Latent(
          "examples.SelectedWorkItem", 0, net.bytebuddy.description.type.TypeDescription.ForLoadedType.of(Object.class).asGenericType());
      assertTrue(instrumentation.typeMatcher().matches(selected));
      assertFalse(instrumentation.typeMatcher().matches(net.bytebuddy.description.type.TypeDescription.ForLoadedType.of(String.class)));
      var matchers = new java.util.ArrayList<net.bytebuddy.matcher.ElementMatcher<? super net.bytebuddy.description.method.MethodDescription>>();
      instrumentation.transform(new io.opentelemetry.javaagent.extension.instrumentation.TypeTransformer() {
        @Override public void applyAdviceToMethod(
            net.bytebuddy.matcher.ElementMatcher<? super net.bytebuddy.description.method.MethodDescription> matcher,
            java.util.function.Function<net.bytebuddy.asm.Advice.WithCustomMapping, net.bytebuddy.asm.Advice.WithCustomMapping> mapping,
            String advice) { matchers.add(matcher); }
        @Override public void applyTransformer(net.bytebuddy.agent.builder.AgentBuilder.Transformer transformer) {
          throw new AssertionError("unexpected transformer");
        }
      });
      var constructor = new net.bytebuddy.description.method.MethodDescription.ForLoadedConstructor(Object.class.getConstructor());
      var close = new net.bytebuddy.description.method.MethodDescription.ForLoadedMethod(AutoCloseable.class.getMethod("close"));
      assertTrue(matchers.get(0).matches(constructor));
      assertFalse(matchers.get(0).matches(close));
      assertTrue(matchers.get(1).matches(close));
      assertFalse(matchers.get(1).matches(constructor));
      LifetimeModule.Constructed.exit(new Object(), "unmatched-type");
      LifetimeModule.Released.exit(new Object(), new IllegalStateException("release failed"));
      LifetimeModule.Released.exit(new Object(), null);
    } finally {
      System.clearProperty("otelc.lifetime.types");
    }
  }

  private ReferenceQueue<TestResource> observeResources() {
    ReferenceQueue<TestResource> collected = new ReferenceQueue<>();
    TestResource first = new TestResource();
    TestResource second = new TestResource();
    TestResource overflow = new TestResource();
    // Keep weak-reference registrations alive until the test has seen GC events.
    references.add(new WeakReference<>(first, collected));
    references.add(new WeakReference<>(second, collected));
    Observer.constructed(first, "mismatch");
    Observer.constructed(first, first.getClass().getName());
    Observer.constructed(first, first.getClass().getName());
    Observer.constructed(second, second.getClass().getName());
    Observer.constructed(overflow, overflow.getClass().getName());
    assertEquals(1, Observer.lost());
    Observer.released(new Object());
    Observer.released(first);
    Observer.released(first);
    assertEquals(1, count("otelc.lifetime.completed"));
    return collected;
  }

  private final List<WeakReference<TestResource>> references = new java.util.ArrayList<>();

  @Test void weakLifetimesDoNotCallApplicationHashCodeAndReportCapacity() throws Exception {
    ReferenceQueue<TestResource> collected = observeResources();
    LiveState state = new LiveState(new Properties());
    System.setProperty("otelc.lifetime.types", TestResource.class.getName());
    try {
      state.command("off");
      assertTrue(state.command("status").contains("\"lifetime_lost\":1"),
          "capacity health must remain visible while metric collection and emission are off");
    } finally {
      System.clearProperty("otelc.lifetime.types");
    }
    assertTrue(state.command("status").contains("\"lifetime_lost\":0"),
        "an empty resolved selection cannot report an unavailable observer");
    long deadline = System.nanoTime() + 5_000_000_000L;
    // GC completion and missing-release accounting are separate observer updates.
    while ((count("otelc.lifetime.completed") < 3 || count("otelc.lifetime.incomplete") < 1)
        && System.nanoTime() < deadline) {
      System.gc();
      collected.remove(20);
    }
    assertEquals(3, count("otelc.lifetime.completed"));
    assertEquals(1, count("otelc.lifetime.incomplete"));
    assertTrue(references.stream().allMatch(reference -> reference.get() == null));
  }

  @Test void officialAutoconfigureAndAgentListenerLoadTheExtension() throws Exception {
    var builder = AutoConfiguredOpenTelemetrySdk.builder().disableShutdownHook()
        .addPropertiesSupplier(() -> Map.of("otel.metrics.exporter", "otlp", "otel.logs.exporter", "none",
            "otel.traces.exporter", "none", "otel.exporter.otlp.protocol", "http/protobuf", "otel.metric.export.interval", "3600000"));
    Extension extension = new Extension();
    extension.customize(builder);
    var configured = builder.build();
    try (var sdk = configured.getOpenTelemetrySdk()) {
      extension.afterAgent(configured);
      assertTrue(Files.exists(directory.resolve("agent.sock")));
    }
  }

  @Test void privateControlRejectsIncompleteConnectionsAndShutsDown() throws Exception {
    LiveState state = new LiveState(new Properties());
    Path socket = directory.resolve("control.sock");
    try (ServerSocketChannel listener = ControlServer.start(socket, state)) {
      assertEquals(PosixFilePermissions.fromString("rw-------"), Files.getPosixFilePermissions(socket));
      try (SocketChannel client = SocketChannel.open(UnixDomainSocketAddress.of(socket))) {
        client.write(StandardCharsets.US_ASCII.encode("off\n"));
        ByteBuffer buffer = ByteBuffer.allocate(2048);
        assertTrue(client.read(buffer) > 0);
        assertFalse(state.collecting());
      }
      try (SocketChannel client = SocketChannel.open(UnixDomainSocketAddress.of(socket))) {
        client.write(StandardCharsets.US_ASCII.encode("unterminated"));
        ByteBuffer buffer = ByteBuffer.allocate(2048);
        assertTrue(client.read(buffer) > 0);
        assertTrue(new String(buffer.array(), 0, buffer.position(), StandardCharsets.US_ASCII).contains("incomplete"));
      }
    }
    Path publicDirectory = Files.createDirectory(directory.resolve("public"));
    Files.setPosixFilePermissions(publicDirectory, PosixFilePermissions.fromString("rwxr-xr-x"));
    assertThrows(java.io.IOException.class, () -> ControlServer.start(publicDirectory.resolve("bad.sock"), state));
  }
}
