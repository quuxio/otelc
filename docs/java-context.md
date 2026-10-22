# Java task and HTTP context propagation

<!-- markdownlint-disable MD013 -->

The common policy enables task and HTTP propagation separately. These are launch-time choices; the installed metric collection and emission switches remain live and independent. Propagation can remain enabled with `traces.enabled=false`: the agent preserves unsampled context and HTTP metrics without exporting spans. The context defaults are both false. Setting a language name in this policy does not implement its adapter: this rebuild currently supports Java.

```toml
[traces]
enabled = true
sample_ratio = 1.0

[context]
tasks = true
http = true
```

The Java translator enables the pinned official agent's `executors`, `java-http-client` and `java-http-server` modules. HTTP uses the official `tracecontext,baggage` propagators. All other library instrumentations remain disabled by default. Source/function selections still control our direct probes; enabling HTTP separately opts into the supported library boundaries, including when application source is excluded. Developers add no SDK imports, annotations or bootstrap.

## Run the ordinary examples

Use JDK 21 and the [local Collector/Grafana stack](../README.md#java-example-and-grafana). Build the examples and their digest-bound prepared manifests:

```sh
make setup java-context-examples
make stack-up
```

The [task application](../examples/java/TaskApp.java) contains ordinary Java code such as:

```java
static int parent(int value) throws Exception {
  return POOL.submit(() -> child(value)).get();
}
```

Launch it with the common [context policy](../examples/context.toml):

```sh
.venv/bin/python -m otelc --config examples/context.toml java \
  --manifest build/examples/context/java/TaskApp.json \
  --runtime /tmp/otelc-tasks-$$
```

Enter `run`, `outside`, `cancel`, `reject`, `error`, then `quit`. The first four results are `8`, `21`, `true`, `true`. The error result reports preserved throwable identity and the number of ordinary exception-formatting observations. With task propagation on, the selected child span belongs to its selected parent; a later unrelated call on the reused worker starts a new trace. Cancelled and rejected work does not execute the selected child. Add `--plain` before the application arguments to run without the agent.

The [HTTP application](../examples/java/HttpContextApp.java) uses JDK `HttpClient` and `HttpServer`. In one terminal:

```sh
.venv/bin/python -m otelc --config examples/context.toml java \
  --manifest build/examples/context/java/HttpContextApp.json \
  --runtime /tmp/otelc-http-server-$$ -- server /tmp/otelc-http-requests-$$.txt
```

It prints `ready:PORT`. In a second terminal, replace `PORT` with that number:

```sh
.venv/bin/python -m otelc --config examples/context.toml java \
  --manifest build/examples/context/java/HttpContextApp.json \
  --runtime /tmp/otelc-http-client-$$ -- client http://127.0.0.1:PORT
```

Enter `/ok` and `/error`; the response summaries start with `200:8` and `500:bad`. They also report the unchanged original request header view, identity/hash observations and copied headers. Enter `quit` in each terminal to stop. The server's audit file deliberately records received carrier headers so wire propagation can be checked. Its optional third argument is a downstream URL; `/relay` forwards to it while retaining trace context and baggage.

Use the selected runtime path with the [live metric commands](../README.md#collection-emission-and-lifetimes). No task/HTTP trace setting changes when metrics turn off. The [Grafana function dashboard](http://localhost:3001/d/otelc-lifetimes) shows the selected function metrics. The [task and service trace dashboard](http://localhost:3001/d/otelc-traces) works with the local anonymous Viewer: select the service and time range, then click a trace name to view its span tree. Its default service is `otelc-context-examples`. Explore is unavailable to this Viewer; it is not needed. Alternatively, retrieve stored trace IDs through Tempo's loopback API:

```sh
curl --fail --get --data-urlencode 'q={resource.service.name="otelc-context-examples"}' \
  http://localhost:3201/api/search
```

Read an individual result with `curl --fail http://localhost:3201/api/traces/TRACE_ID`, replacing `TRACE_ID` with a returned `traceID`. Client/server duration metrics are exported through the same SDK and obey the collection/emission switches. The equivalent-workload benchmark stays on its existing policy; these diagnostic applications are not cross-language timing comparisons.

## Preservation and tested boundaries

The official agent's pinned [HTTP header getter advice](https://github.com/open-telemetry/opentelemetry-java-instrumentation/blob/v2.32.0/instrumentation/java-http-client/javaagent/src/main/java/io/opentelemetry/javaagent/instrumentation/javahttpclient/HttpHeadersInstrumentation.java) injects carrier headers into the returned view of `HttpRequest.headers()`. Without a guard, application header reads, copies and hashes can see instrumentation headers inside an active span. A focused regression reproduced that change before this fix.

Our small `otelc-http-headers` module preserves the original header-field reference on both the ordinary immutable request and the JDK transport copy returned by `HttpResponse.request()`. A second regression caught a leak through that response request even after original-request reads were preserved. The module captures only carrier strings through the SDK's configured propagator when the transport request is constructed, and combines them with headers only at the private `getUserHeaders()` transport boundary. No application object is copied, no request field changes, and no extra application getter is invoked. The official client/server instrumenters still create and complete HTTP spans.

Carrier metadata uses the pinned instrumentation API's [weak identity cache](https://github.com/open-telemetry/opentelemetry-java-instrumentation/blob/v2.32.0/instrumentation-api/src/main/java/io/opentelemetry/instrumentation/api/internal/cache/Cache.java). Its keys are weak and compared by identity; values contain only strings, not requests or SDK contexts. This is a pinned internal SDK API, covered by real-agent and getter-trap tests, and must be requalified on upgrades. The cache has no fixed entry cap; metadata follows reachable transport requests and is reclaimed after their collection. It adds no application fields. Helper namespace relocation covers the helper and carrier record. Preflight refuses incompatible public/private request-header layouts before application launch.

Real JDK 21 JVM comparisons cover single-platform-worker `submit`/`FutureTask`, reuse, cancellation before execution, failed submission and thrown identity; synchronous HTTP/1.1 client/server, 500 responses, incoming sampled/unsampled context, server-to-downstream relay and baggage; and original request headers, header identity, hash, copied header maps and the response request's public headers. Decoded OTLP checks exact parent/trace identities and official HTTP duration counts. All four metric states are exercised in the same instrumented PIDs, including startup-off. Complete application/tooling input identities are rechecked after execution.

The official modules cover more integrations than this qualified set. This change does not claim qualification of custom `HttpRequest` subclasses, redirects/retries, asynchronous HTTP/cancellation, HTTP/2, arbitrary executor wrappers, virtual/structured threads, custom classloaders or other JDK layouts. Those remain explicit follow-up qualification and implementation work. Task/HTTP propagation also does not enable asynchronous per-action witness attribution.
