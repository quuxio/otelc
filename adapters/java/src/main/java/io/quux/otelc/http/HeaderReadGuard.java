package io.quux.otelc.http;

import io.opentelemetry.api.GlobalOpenTelemetry;
import io.opentelemetry.context.Context;
import io.opentelemetry.context.propagation.TextMapPropagator;
import io.opentelemetry.instrumentation.api.internal.cache.Cache;
import java.net.http.HttpHeaders;
import java.net.http.HttpRequest;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/** Keep public header fields unchanged; only the private wire getter receives SDK carrier values. */
public final class HeaderReadGuard {
  private static final Cache<HttpRequest, Carrier> CARRIERS = Cache.weak();
  private HeaderReadGuard() {}

  public static void capture(HttpRequest request) {
    capture(request, Context.current(), GlobalOpenTelemetry.getPropagators().getTextMapPropagator());
  }

  static void capture(HttpRequest request, Context context, TextMapPropagator propagator) {
    Map<String, List<String>> carrier = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
    propagator.inject(context, carrier, (values, key, value) -> values.put(key, List.of(value)));
    // The weak-key SDK store retains only strings, never an application request or an SDK Context.
    if (carrier.isEmpty()) CARRIERS.remove(request);
    else CARRIERS.put(request, new Carrier(Map.copyOf(carrier)));
  }

  public static HttpHeaders wireHeaders(HttpRequest request, HttpHeaders original) {
    Carrier carrier = CARRIERS.get(request);
    if (carrier == null) return original;
    Map<String, List<String>> values = new TreeMap<>(String.CASE_INSENSITIVE_ORDER);
    values.putAll(original.map());
    values.putAll(carrier.headers());
    return HttpHeaders.of(values, (key, value) -> true);
  }

  public record Carrier(Map<String, List<String>> headers) {}
}
