package io.quux.otelc.witness;

import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.data.SpanData;
import io.opentelemetry.sdk.trace.export.SpanExporter;
import java.util.Collection;

/** Observe the real SDK pipeline, including completions on exporter worker threads. */
public final class WitnessPipeline {
  private WitnessPipeline() {}

  public static SpanProcessor processor(SpanProcessor delegate, WitnessRuntime runtime) {
    return new SpanProcessor() {
      @Override public boolean isStartRequired() { return true; }
      @Override public boolean isEndRequired() { return true; }
      @Override public void onStart(Context parent, ReadWriteSpan span) {
        runtime.spanStarted(span);
        if (delegate.isStartRequired()) delegate.onStart(parent, span);
      }
      @Override public void onEnd(ReadableSpan span) {
        runtime.spanEnded(span);
        if (delegate.isEndRequired()) delegate.onEnd(span);
      }
      @Override public CompletableResultCode forceFlush() { return delegate.forceFlush(); }
      @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
    };
  }

  public static SpanExporter exporter(SpanExporter delegate, WitnessRuntime runtime) {
    return new SpanExporter() {
      @Override public CompletableResultCode export(Collection<SpanData> spans) {
        var receipt = runtime.exporting(spans);
        try {
          CompletableResultCode result = delegate.export(spans);
          result.whenComplete(() -> runtime.exported(receipt, result.isSuccess()));
          return result;
        } catch (RuntimeException error) {
          runtime.exported(receipt, false);
          throw error;
        }
      }
      @Override public CompletableResultCode flush() { return delegate.flush(); }
      @Override public CompletableResultCode shutdown() { return delegate.shutdown(); }
    };
  }
}
