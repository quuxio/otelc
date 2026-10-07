package quux.otelc.java;

import io.opentelemetry.exporter.internal.otlp.traces.TraceRequestMarshaler;
import io.opentelemetry.proto.collector.trace.v1.ExportTraceServiceResponse;
import io.opentelemetry.sdk.trace.data.SpanData;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;
import java.util.Collection;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/** Pinned SDK encoding with strict bounded OTLP acknowledgements and retries. */
final class TraceExporter implements AutoCloseable {
  private final TraceStore store;
  private final HttpClient client;
  private final Map<String, String> headers;
  private volatile CompletableFuture<?> active;
  private volatile boolean closed;
  TraceExporter(TraceStore store) {
    this.store = store;
    headers = Exporter.headers(System.getenv(), "OTEL_EXPORTER_OTLP_TRACES_HEADERS");
    client = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).connectTimeout(Duration.ofMillis(store.timeoutMs)).build();
  }
  CompletableFuture<Boolean> export(Collection<SpanData> spans, long deadline) {
    try {
      var bytes = new ByteArrayOutputStream();
      // Internal SDK marshaler is intentionally pinned to the metrics SDK version.
      TraceRequestMarshaler.create(spans).writeBinaryTo(bytes);
      if (bytes.size() > TraceStore.MAX_BYTES) return CompletableFuture.completedFuture(false);
      return attempt(bytes.toByteArray(), deadline, 3);
    } catch (Exception ignored) { return CompletableFuture.completedFuture(false); }
  }
  private CompletableFuture<Boolean> attempt(byte[] bytes, long deadline, int remaining) {
    long budget = deadline - System.nanoTime();
    if (closed || budget <= 0) return CompletableFuture.completedFuture(false);
    var builder = HttpRequest.newBuilder(URI.create(store.endpoint)).timeout(Duration.ofNanos(budget)).header("Content-Type", "application/x-protobuf");
    headers.forEach(builder::header);
    var future = client.sendAsync(builder.POST(HttpRequest.BodyPublishers.ofByteArray(bytes)).build(), ignored -> new Exporter.LimitedBody());
    active = future;
    return future.orTimeout(budget, TimeUnit.NANOSECONDS).handle((response, failure) -> {
      if (failure != null) { future.cancel(true); return remaining > 1 && retryable(failure) ? attempt(bytes, deadline, remaining - 1) : CompletableFuture.completedFuture(false); }
      if (response.statusCode() == 200) {
        try { return CompletableFuture.completedFuture(ExportTraceServiceResponse.parseFrom(response.body()).getPartialSuccess().getRejectedSpans() == 0); }
        catch (Exception ignored) { return CompletableFuture.completedFuture(false); }
      }
      return remaining > 1 && java.util.Set.of(429, 502, 503, 504).contains(response.statusCode()) ? attempt(bytes, deadline, remaining - 1) : CompletableFuture.completedFuture(false);
    }).thenCompose(value -> value);
  }
  private static boolean retryable(Throwable failure) {
    for (int depth = 0; depth < 16 && failure.getCause() != null; depth++) {
      if (failure instanceof IllegalArgumentException) return false;
      failure = failure.getCause();
    }
    return failure instanceof java.io.IOException;
  }
  @Override public void close() {
    closed = true;
    var request = active; if (request != null && !request.isDone()) request.cancel(true);
    client.shutdownNow();
  }
}
