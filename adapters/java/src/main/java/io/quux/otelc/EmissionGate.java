package io.quux.otelc;

import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.metrics.Aggregation;
import io.opentelemetry.sdk.metrics.InstrumentType;
import io.opentelemetry.sdk.metrics.data.AggregationTemporality;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.metrics.export.MetricExporter;
import java.util.Collection;

/** Suppress delegate calls without replacing SDK instruments or retaining batches. */
public final class EmissionGate implements MetricExporter {
  static final class State {
    private boolean enabled;
    private int inFlight;
    State(boolean enabled) { this.enabled = enabled; }
    synchronized boolean enabled() { return enabled; }
    synchronized int inFlight() { return inFlight; }
    synchronized void set(boolean value) { enabled = value; }
    synchronized boolean begin() { if (!enabled) return false; inFlight++; return true; }
    synchronized void end() { inFlight--; }
  }

  private final MetricExporter delegate;
  private final State state;
  EmissionGate(MetricExporter delegate, State state) { this.delegate = delegate; this.state = state; }

  @Override public CompletableResultCode export(Collection<MetricData> metrics) {
    if (!state.begin()) return CompletableResultCode.ofSuccess();
    try {
      CompletableResultCode result = delegate.export(metrics);
      result.whenComplete(state::end);
      return result;
    } catch (RuntimeException error) {
      state.end();
      throw error;
    }
  }
  @Override public CompletableResultCode flush() {
    return state.enabled() ? delegate.flush() : CompletableResultCode.ofSuccess();
  }
  @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
  @Override public AggregationTemporality getAggregationTemporality(InstrumentType type) {
    return delegate.getAggregationTemporality(type);
  }
  @Override public Aggregation getDefaultAggregation(InstrumentType type) {
    return delegate.getDefaultAggregation(type);
  }
}
