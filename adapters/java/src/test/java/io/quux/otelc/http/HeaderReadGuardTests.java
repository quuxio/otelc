package io.quux.otelc.http;

import static org.junit.jupiter.api.Assertions.*;

import io.opentelemetry.api.baggage.Baggage;
import io.opentelemetry.api.baggage.propagation.W3CBaggagePropagator;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import io.opentelemetry.api.trace.TraceFlags;
import io.opentelemetry.api.trace.TraceState;
import io.opentelemetry.api.trace.propagation.W3CTraceContextPropagator;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.propagation.TextMapPropagator;
import java.lang.ref.WeakReference;
import java.net.URI;
import java.net.http.HttpHeaders;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;
import java.util.Optional;
import org.junit.jupiter.api.Test;

class HeaderReadGuardTests {
  @Test void capturesOnlyCarrierValuesAndKeepsOriginalViewsAndRequestsUnchanged() {
    HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost/"))
        .header("TraceParent", "ordinary").header("existing", "value").build();
    var original = request.headers();
    var span = SpanContext.create("11111111111111111111111111111111", "2222222222222222",
        TraceFlags.getSampled(), TraceState.getDefault());
    var context = Baggage.builder().put("session", "ordinary").build()
        .storeInContext(Context.root().with(Span.wrap(span)));
    var propagator = TextMapPropagator.composite(W3CTraceContextPropagator.getInstance(), W3CBaggagePropagator.getInstance());
    assertSame(original, HeaderReadGuard.wireHeaders(request, original));
    HeaderReadGuard.capture(request, context, propagator);
    var wire = HeaderReadGuard.wireHeaders(request, original);
    assertEquals(java.util.List.of("00-11111111111111111111111111111111-2222222222222222-01"),
        wire.allValues("traceparent"));
    assertEquals("session=ordinary", wire.firstValue("baggage").orElseThrow());
    assertEquals("value", wire.firstValue("existing").orElseThrow());
    assertSame(original, request.headers());
    assertEquals("ordinary", request.headers().firstValue("traceparent").orElseThrow());
    HeaderReadGuard.capture(request, Context.root(), propagator);
    assertSame(original, HeaderReadGuard.wireHeaders(request, original));
    HeaderReadGuard.capture(request);
  }

  @Test void metadataNeverCallsRequestGettersHashCodeOrEquals() {
    HttpRequest request = new HttpRequest() {
      @Override public Optional<BodyPublisher> bodyPublisher() { throw new AssertionError("bodyPublisher called"); }
      @Override public String method() { throw new AssertionError("method called"); }
      @Override public Optional<Duration> timeout() { throw new AssertionError("timeout called"); }
      @Override public boolean expectContinue() { throw new AssertionError("expectContinue called"); }
      @Override public URI uri() { throw new AssertionError("uri called"); }
      @Override public Optional<HttpClient.Version> version() { throw new AssertionError("version called"); }
      @Override public HttpHeaders headers() { throw new AssertionError("headers/hashCode/equals called"); }
    };
    HttpHeaders original = HttpHeaders.of(java.util.Map.of(), (key, value) -> true);
    var span = SpanContext.create("33333333333333333333333333333333", "4444444444444444",
        TraceFlags.getDefault(), TraceState.getDefault());
    HeaderReadGuard.capture(request, Context.root().with(Span.wrap(span)), W3CTraceContextPropagator.getInstance());
    assertEquals("00-33333333333333333333333333333333-4444444444444444-00",
        HeaderReadGuard.wireHeaders(request, original).firstValue("traceparent").orElseThrow());
    HeaderReadGuard.capture(request, Context.root(), TextMapPropagator.noop());
    assertSame(original, HeaderReadGuard.wireHeaders(request, original));
  }

  private static WeakReference<HttpRequest> capturedRequest() {
    HttpRequest request = HttpRequest.newBuilder(URI.create("http://localhost/collectable")).build();
    var span = SpanContext.create("55555555555555555555555555555555", "6666666666666666",
        TraceFlags.getSampled(), TraceState.getDefault());
    HeaderReadGuard.capture(request, Context.root().with(Span.wrap(span)), W3CTraceContextPropagator.getInstance());
    assertTrue(HeaderReadGuard.wireHeaders(request, request.headers()).firstValue("traceparent").isPresent());
    return new WeakReference<>(request);
  }

  @Test void carrierMetadataDoesNotKeepItsRequestAlive() throws InterruptedException {
    WeakReference<HttpRequest> reference = capturedRequest();
    for (int attempt = 0; attempt < 100 && reference.get() != null; attempt++) {
      // Explicit collection belongs only to this isolated retention regression fixture.
      System.gc();
      Thread.sleep(10);
    }
    assertNull(reference.get(), "carrier metadata retained the request");
  }
}
