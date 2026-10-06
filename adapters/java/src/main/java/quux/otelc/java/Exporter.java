package quux.otelc.java;

import io.opentelemetry.exporter.internal.otlp.metrics.MetricsRequestMarshaler;
import io.opentelemetry.proto.collector.metrics.v1.ExportMetricsServiceResponse;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.metrics.InstrumentType;
import io.opentelemetry.sdk.metrics.data.AggregationTemporality;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.metrics.data.LongPointData;
import io.opentelemetry.sdk.metrics.data.HistogramPointData;
import io.opentelemetry.sdk.metrics.export.MetricExporter;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.URLDecoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Collection;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.Flow;
import java.util.concurrent.atomic.AtomicBoolean;

/** SDK encoding plus a bounded transport: one request, strict ACKs, no redirects. */
final class Exporter implements MetricExporter {
  private final Telemetry runtime;
  private final HttpClient client;
  private final Map<String, String> headers;
  private final AtomicBoolean busy = new AtomicBoolean();
  private volatile CompletableFuture<?> active;
  private volatile String signature;
  Exporter(Telemetry runtime) {
    this.runtime = runtime;
    headers = headers(System.getenv());
    client = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).connectTimeout(Duration.ofMillis(runtime.timeoutMs)).build();
  }
  static Map<String, String> headers(Map<String, String> environment) {
    var result = new HashMap<String, String>();
    String source = environment.getOrDefault("OTEL_EXPORTER_OTLP_METRICS_HEADERS", environment.getOrDefault("OTEL_EXPORTER_OTLP_HEADERS", ""));
    for (String item : source.split(",")) {
      if (item.isBlank()) continue;
      int separator = item.indexOf('=');
      if (separator < 1) throw new IllegalArgumentException("invalid OTLP headers");
      String name = URLDecoder.decode(item.substring(0, separator).trim().replace("+", "%2B"), StandardCharsets.UTF_8);
      String value = URLDecoder.decode(item.substring(separator + 1).trim().replace("+", "%2B"), StandardCharsets.UTF_8);
      HttpRequest.newBuilder(URI.create("http://localhost")).header(name, value);
      result.put(name, value);
    }
    return result;
  }
  @Override public AggregationTemporality getAggregationTemporality(InstrumentType type) { return AggregationTemporality.CUMULATIVE; }
  @Override public CompletableResultCode export(Collection<MetricData> data) {
    String current = data.stream().filter(metric -> !metric.getName().equals("otelc.export.dropped_batches")).map(metric -> metric.getName() + metric.getData().getPoints().stream().map(point -> point.getAttributes().toString() + ":" + (point instanceof LongPointData value ? value.getValue() : point instanceof HistogramPointData value ? value.getCount() : "unsupported")).toList()).sorted().toList().toString();
    if (current.equals(signature)) return CompletableResultCode.ofSuccess();
    if (!busy.compareAndSet(false, true)) { runtime.exportLoss.incrementAndGet(); return CompletableResultCode.ofFailure(); }
    var result = new CompletableResultCode();
    try {
      var bytes = new ByteArrayOutputStream();
      // This SDK marshaler API is internal; its exact package/version is pinned.
      MetricsRequestMarshaler.create(data).writeBinaryTo(bytes);
      var request = HttpRequest.newBuilder(URI.create(runtime.plan.data.get("metrics_endpoint").getAsString())).timeout(Duration.ofMillis(runtime.timeoutMs)).header("Content-Type", "application/x-protobuf");
      headers.forEach(request::header);
      var future = client.sendAsync(request.POST(HttpRequest.BodyPublishers.ofByteArray(bytes.toByteArray())).build(), ignored -> new LimitedBody());
      active = future;
      future.thenApply(response -> response).orTimeout(runtime.timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS).whenComplete((response, failure) -> {
        boolean success = false;
        try {
          if (failure == null && response.statusCode() == 200) success = ExportMetricsServiceResponse.parseFrom(response.body()).getPartialSuccess().getRejectedDataPoints() == 0;
        } catch (Exception ignored) { /* Invalid acknowledgements are failures. */ }
        busy.set(false);
        if (success) { signature = current; result.succeed(); }
        else { runtime.exportLoss.incrementAndGet(); future.cancel(true); result.fail(); }
      });
    } catch (Exception ignored) { busy.set(false); runtime.exportLoss.incrementAndGet(); result.fail(); }
    return result;
  }
  @Override public CompletableResultCode flush() { return CompletableResultCode.ofSuccess(); }
  @Override public CompletableResultCode shutdown() {
    var request = active;
    if (request != null && !request.isDone()) request.cancel(true);
    client.shutdownNow();
    return CompletableResultCode.ofSuccess();
  }
  private static final class LimitedBody implements HttpResponse.BodySubscriber<byte[]> {
    private final CompletableFuture<byte[]> result = new CompletableFuture<>();
    private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
    private Flow.Subscription subscription;
    @Override public CompletionStage<byte[]> getBody() { return result; }
    @Override public void onSubscribe(Flow.Subscription value) { subscription = value; value.request(1); }
    @Override public void onNext(List<ByteBuffer> chunks) {
      for (var chunk : chunks) {
        if ((long) bytes.size() + chunk.remaining() > 65536) { subscription.cancel(); result.completeExceptionally(new IllegalArgumentException("OTLP response exceeds limit")); return; }
        var value = new byte[chunk.remaining()]; chunk.get(value); bytes.writeBytes(value);
      }
      subscription.request(1);
    }
    @Override public void onError(Throwable failure) { result.completeExceptionally(failure); }
    @Override public void onComplete() { result.complete(bytes.toByteArray()); }
  }
}
