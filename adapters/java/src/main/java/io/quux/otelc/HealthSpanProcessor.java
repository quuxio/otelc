package io.quux.otelc;

import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.trace.ReadWriteSpan;
import io.opentelemetry.sdk.trace.ReadableSpan;
import io.opentelemetry.sdk.trace.SpanProcessor;
import io.opentelemetry.sdk.trace.export.BatchSpanProcessor;

/** Account before enqueue, so queue rejection remains visible independently of metrics. */
final class HealthSpanProcessor implements SpanProcessor {
  private final SpanProcessor delegate;
  private final RuntimeHealth health;

  HealthSpanProcessor(SpanProcessor delegate, RuntimeHealth health) {
    this.delegate = delegate;
    this.health = health;
    health.processorInstalled(delegate instanceof BatchSpanProcessor);
  }

  @Override public boolean isStartRequired() { return true; }
  @Override public boolean isEndRequired() { return true; }
  @Override public void onStart(Context parent, ReadWriteSpan span) {
    if (span.getSpanContext().isSampled()) health.spanStarted();
    if (delegate.isStartRequired()) delegate.onStart(parent, span);
  }
  @Override public void onEnd(ReadableSpan span) {
    if (span.getSpanContext().isSampled()) health.spanEnded();
    if (delegate.isEndRequired()) delegate.onEnd(span);
  }
  @Override public CompletableResultCode forceFlush() { return delegate.forceFlush(); }
  @Override public CompletableResultCode shutdown() {
    CompletableResultCode result = delegate.shutdown();
    result.whenComplete(() -> health.processorClosed(result.isSuccess()));
    return result;
  }
}
