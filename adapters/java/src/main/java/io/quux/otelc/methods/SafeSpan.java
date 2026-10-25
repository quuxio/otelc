package io.quux.otelc.methods;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.StatusCode;
import java.util.concurrent.TimeUnit;

/** Preserve the SDK span, replacing only exception formatting with final JVM identity. */
public final class SafeSpan implements Span {
  private final Span delegate;

  public SafeSpan(Span delegate) { this.delegate = delegate; }

  Span original() { return delegate; }

  @Override public Span recordException(Throwable error, Attributes attributes) {
    if (error != null) delegate.addEvent("exception", attributes.toBuilder()
        .put("exception.type", error.getClass().getName()).build());
    return this;
  }
  @Override public <T> Span setAttribute(AttributeKey<T> key, T value) {
    delegate.setAttribute(key, value); return this;
  }
  @Override public Span addEvent(String name, Attributes attributes) {
    delegate.addEvent(name, attributes); return this;
  }
  @Override public Span addEvent(String name, Attributes attributes, long timestamp, TimeUnit unit) {
    delegate.addEvent(name, attributes, timestamp, unit); return this;
  }
  @Override public Span setStatus(StatusCode status, String description) {
    delegate.setStatus(status, description); return this;
  }
  @Override public Span updateName(String name) { delegate.updateName(name); return this; }
  @Override public Span addLink(SpanContext context, Attributes attributes) {
    delegate.addLink(context, attributes); return this;
  }
  @Override public void end() { delegate.end(); }
  @Override public void end(long timestamp, TimeUnit unit) { delegate.end(timestamp, unit); }
  @Override public SpanContext getSpanContext() { return delegate.getSpanContext(); }
  @Override public boolean isRecording() { return delegate.isRecording(); }
}
