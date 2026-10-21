package io.quux.otelc;

import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.common.export.MemoryMode;
import io.opentelemetry.sdk.metrics.Aggregation;
import io.opentelemetry.sdk.metrics.InstrumentType;
import io.opentelemetry.sdk.metrics.data.AggregationTemporality;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.metrics.export.MetricExporter;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.Collection;
import java.util.function.Supplier;

/** Observe exporter API results without retaining batches or replacing transport. */
final class ExportHealth {
  private ExportHealth() {}

  private static CompletableResultCode track(RuntimeHealth health, RuntimeHealth.Signal signal,
      long units, Supplier<CompletableResultCode> action) {
    health.exportStarted(signal, units);
    try {
      CompletableResultCode result = action.get();
      result.whenComplete(() -> health.exportFinished(signal, units, result.isSuccess()));
      return result;
    } catch (RuntimeException error) {
      health.exportFinished(signal, units, false);
      throw error;
    }
  }

  static SpanExporter spans(SpanExporter delegate, RuntimeHealth health) {
    health.exporterInstalled(RuntimeHealth.Signal.TRACES);
    return new SpanExporter() {
      @Override public CompletableResultCode export(Collection<SpanData> spans) {
        return track(health, RuntimeHealth.Signal.TRACES, spans.size(), () -> delegate.export(spans));
      }
      @Override public CompletableResultCode flush() { return delegate.flush(); }
      @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
    };
  }

  static MetricExporter metrics(MetricExporter delegate, RuntimeHealth health) {
    health.exporterInstalled(RuntimeHealth.Signal.METRICS);
    return new MetricExporter() {
      @Override public CompletableResultCode export(Collection<MetricData> metrics) {
        long points = metrics.stream().mapToLong(metric -> metric.getData().getPoints().size()).sum();
        return track(health, RuntimeHealth.Signal.METRICS, points, () -> delegate.export(metrics));
      }
      @Override public CompletableResultCode flush() { return delegate.flush(); }
      @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
      @Override public AggregationTemporality getAggregationTemporality(InstrumentType type) {
        return delegate.getAggregationTemporality(type);
      }
      @Override public Aggregation getDefaultAggregation(InstrumentType type) {
        return delegate.getDefaultAggregation(type);
      }
      @Override public MemoryMode getMemoryMode() { return delegate.getMemoryMode(); }
    };
  }
}
