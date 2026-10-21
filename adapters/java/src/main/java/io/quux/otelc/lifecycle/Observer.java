package io.quux.otelc.lifecycle;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.metrics.DoubleHistogram;
import io.opentelemetry.api.metrics.LongCounter;
import io.opentelemetry.api.metrics.Meter;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.Tracer;
import java.lang.ref.ReferenceQueue;
import java.lang.ref.WeakReference;
import java.time.Instant;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/** Weak identity observation. Never invokes application hashCode, close or GC. */
public final class Observer {
  private static final ReferenceQueue<Object> QUEUE = new ReferenceQueue<>();
  private static final Map<Key, Tracked> LIVE = new HashMap<>();
  private static final int LIMIT = Integer.getInteger("otelc.lifetime.max-live", 4096);
  private static final AtomicLong LOSSES = new AtomicLong();
  private static final boolean TRACES = Boolean.getBoolean("otelc.traces.enabled");
  private static final Set<String> RESOURCES = Set.of(System.getProperty("otelc.lifetime.resources", "").split(","));
  private static final Meter METER = GlobalOpenTelemetry.getMeter("io.quux.otelc.lifetimes");
  private static final Tracer TRACER = GlobalOpenTelemetry.getTracer("io.quux.otelc.lifetimes");
  private static final LongCounter ADMITTED = METER.counterBuilder("otelc.lifetime.admitted").build();
  private static final LongCounter COMPLETED = METER.counterBuilder("otelc.lifetime.completed").build();
  private static final LongCounter INCOMPLETE = METER.counterBuilder("otelc.lifetime.incomplete").build();
  private static final LongCounter LOST = METER.counterBuilder("otelc.lifetime.lost").build();
  private static final DoubleHistogram DURATION = METER.histogramBuilder("otelc.lifetime.duration").setUnit("s").build();

  static {
    METER.gaugeBuilder("otelc.lifetime.live").ofLongs().buildWithCallback(result -> {
      synchronized (LIVE) {
        result.record(LIVE.size(), labels("all", "observed_gc"));
        result.record(LIVE.values().stream().filter(value -> value.resource && !value.released).count(), labels("all", "resource"));
      }
    });
    Thread.ofPlatform().daemon().name("otelc-lifetime-observer").start(() -> {
      while (!Thread.currentThread().isInterrupted()) {
        try { collected((Key) QUEUE.remove()); }
        catch (InterruptedException error) { Thread.currentThread().interrupt(); }
      }
    });
  }

  private Observer() {}

  public static void constructed(Object object, String type) {
    boolean metrics = ADMITTED.isEnabled();
    if (!metrics && !TRACES) return;
    if (!object.getClass().getName().equals(type)) return;
    synchronized (LIVE) {
      Key lookup = new Key(object, null);
      if (LIVE.containsKey(lookup)) return;
      if (LIVE.size() >= LIMIT) { LOSSES.incrementAndGet(); LOST.add(1, labels(type, "capacity")); return; }
      Instant now = Instant.now();
      LIVE.put(new Key(object, QUEUE), new Tracked(type, System.nanoTime(),
          now.getEpochSecond() * 1_000_000_000L + now.getNano(), Span.current().getSpanContext(), metrics, RESOURCES.contains(type)));
    }
    if (metrics) {
      ADMITTED.add(1, labels(type, "observed_gc"));
      if (RESOURCES.contains(type)) ADMITTED.add(1, labels(type, "resource"));
    }
  }

  public static void released(Object object) {
    Tracked tracked;
    synchronized (LIVE) {
      tracked = LIVE.get(new Key(object, null));
      if (tracked == null || !tracked.resource || tracked.released) return;
      tracked.released = true;
    }
    complete(tracked, "resource");
  }

  /** Local health remains observable even while SDK metric recording is off. */
  public static long lost() { return LOSSES.get(); }

  private static void collected(Key key) {
    Tracked tracked;
    synchronized (LIVE) { tracked = LIVE.remove(key); }
    if (tracked == null) return;
    complete(tracked, "observed_gc");
    if (tracked.resource && !tracked.released && tracked.metrics) INCOMPLETE.add(1, labels(tracked.type, "resource"));
  }

  private static Attributes labels(String type, String boundary) {
    return Attributes.of(AttributeKey.stringKey("type"), type, AttributeKey.stringKey("boundary"), boundary);
  }

  private static void complete(Tracked tracked, String boundary) {
    long elapsed = Math.max(0, System.nanoTime() - tracked.monotonic);
    Attributes labels = labels(tracked.type, boundary);
    if (tracked.metrics) {
      COMPLETED.add(1, labels);
      DURATION.record(elapsed / 1_000_000_000., labels);
    }
    if (!TRACES) return;
    var builder = TRACER.spanBuilder(tracked.type + " lifetime").setNoParent()
        .setAllAttributes(labels).setStartTimestamp(tracked.epoch, TimeUnit.NANOSECONDS);
    if (tracked.parent.isValid()) builder.addLink(tracked.parent);
    builder.startSpan().end(tracked.epoch + elapsed, TimeUnit.NANOSECONDS);
  }

  static final class Key extends WeakReference<Object> {
    private final int hash;
    Key(Object object, ReferenceQueue<Object> queue) { super(object, queue); hash = System.identityHashCode(object); }
    @Override public int hashCode() { return hash; }
    @Override public boolean equals(Object other) {
      if (other == this) return true;
      return other instanceof Key key && get() != null && get() == key.get();
    }
  }

  static final class Tracked {
    final String type;
    final long monotonic;
    final long epoch;
    final SpanContext parent;
    final boolean metrics;
    final boolean resource;
    boolean released;
    Tracked(String type, long monotonic, long epoch, SpanContext parent, boolean metrics, boolean resource) {
      this.type = type; this.monotonic = monotonic; this.epoch = epoch; this.parent = parent; this.metrics = metrics; this.resource = resource;
    }
  }
}
