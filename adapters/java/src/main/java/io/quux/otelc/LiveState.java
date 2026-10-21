package io.quux.otelc;

import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.internal.MeterConfig;
import io.opentelemetry.sdk.metrics.internal.SdkMeterProviderUtil;
import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicLong;

/** Process-local state shared by extension SPIs, never by application properties. */
public final class LiveState {
  private static final class Holder {
    private static final LiveState INSTANCE = load();

    private static LiveState load() {
      Properties properties = new Properties();
      try (Reader reader = Files.newBufferedReader(Path.of(System.getProperty("otelc.plan")))) {
        properties.load(reader);
        return new LiveState(properties);
      } catch (IOException error) {
        throw new IllegalStateException("Cannot read the generated otelc plan", error);
      }
    }
  }

  private volatile boolean collection;
  final EmissionGate.State emission;
  final AtomicLong completed = new AtomicLong();
  private SdkMeterProvider provider;
  private final Properties plan;

  LiveState(Properties plan) {
    this.plan = plan;
    collection = Boolean.parseBoolean(plan.getProperty("metrics.collection", "true"));
    emission = new EmissionGate.State(Boolean.parseBoolean(plan.getProperty("metrics.emission", "true")));
  }

  public static LiveState instance() { return Holder.INSTANCE; }
  public boolean collecting() { return collection; }
  String property(String key) { return plan.getProperty(key); }

  synchronized void attach(SdkMeterProvider value) {
    provider = value;
    updateCollection(collection);
  }

  synchronized void updateCollection(boolean enabled) {
    collection = enabled;
    if (provider != null) {
      SdkMeterProviderUtil.setMeterConfigurator(provider, scope -> enabled ? MeterConfig.enabled() : MeterConfig.disabled());
    }
  }

  private String lifetimeLosses() {
    try {
      Class<?> observer = Class.forName("io.quux.otelc.lifecycle.Observer", false, ClassLoader.getSystemClassLoader());
      return observer.getMethod("lost").invoke(null).toString();
    } catch (ReflectiveOperationException | LinkageError unavailable) {
      return "null";
    }
  }

  synchronized String command(String command) {
    switch (command) {
      case "on" -> { updateCollection(true); emission.set(true); }
      case "off" -> { updateCollection(false); emission.set(false); }
      case "collection on" -> updateCollection(true);
      case "collection off" -> updateCollection(false);
      case "emission on" -> emission.set(true);
      case "emission off" -> emission.set(false);
      case "flush" -> {
        if (provider != null) provider.forceFlush().join(1500, java.util.concurrent.TimeUnit.MILLISECONDS);
      }
      case "status" -> { /* Read current state without modifying it. */ }
      default -> { return "{\"error\":\"unknown command\"}\n"; }
    }
    return "{\"schema_version\":1,\"pid\":" + ProcessHandle.current().pid()
        + ",\"collection\":" + collection + ",\"emission\":" + emission.enabled()
        + ",\"in_flight_exports\":" + emission.inFlight() + ",\"effective_emission_off\":"
        + (!emission.enabled() && emission.inFlight() == 0) + ",\"completed\":" + completed.get()
        + ",\"lifetime_lost\":" + lifetimeLosses() + "}\n";
  }
}
