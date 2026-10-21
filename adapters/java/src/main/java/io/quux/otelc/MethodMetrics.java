package io.quux.otelc;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.metrics.DoubleHistogram;
import io.opentelemetry.api.metrics.LongCounter;
import io.opentelemetry.api.metrics.Meter;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.ContextKey;
import io.opentelemetry.instrumentation.api.incubator.instrumenter.InstrumenterCustomizer;
import io.opentelemetry.instrumentation.api.incubator.instrumenter.InstrumenterCustomizerProvider;
import io.opentelemetry.instrumentation.api.instrumenter.OperationListener;

/** Direct metrics attached to official method hooks, independent of sampled spans. */
public final class MethodMetrics implements InstrumenterCustomizerProvider {
  @Override public void customize(InstrumenterCustomizer customizer) {
    if (customizer.getInstrumentationName().equals("io.opentelemetry.methods")) {
      customizer.addOperationMetrics(meter -> listener(meter, LiveState.instance()));
    }
  }

  static OperationListener listener(Meter meter, LiveState state) {
    LongCounter calls = meter.counterBuilder("otelc.function.calls").setUnit("{call}").build();
    DoubleHistogram duration = meter.histogramBuilder("otelc.function.duration").setUnit("s").build();
    ContextKey<Started> key = ContextKey.named("otelc-method-metrics");
    Started unmeasured = new Started(0, Attributes.empty());
    return new OperationListener() {
      @Override public Context onStart(Context context, Attributes attributes, long nanos) {
        if (!state.collecting()) return context.with(key, unmeasured);
        String function = attributes.get(AttributeKey.stringKey("code.function.name"));
        if (function == null) function = attributes.get(AttributeKey.stringKey("code.function"));
        Attributes labels = Attributes.of(AttributeKey.stringKey("function"), function == null ? "unknown" : function);
        return context.with(key, new Started(nanos, labels));
      }
      @Override public void onEnd(Context context, Attributes attributes, long nanos) {
        Started started = context.get(key);
        if (started == null || started == unmeasured) return;
        calls.add(1, started.labels());
        duration.record(Math.max(0, nanos - started.nanos()) / 1_000_000_000., started.labels());
        state.completed.incrementAndGet();
      }
    };
  }

  private record Started(long nanos, Attributes labels) {}
}
