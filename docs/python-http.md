# Python HTTP context propagation

Enable `propagation.http` to join qualified Python standard-library HTTP clients and servers without changing application source. The adapter creates CLIENT and SERVER spans, propagates W3C `traceparent`/`tracestate` and parents selected functions inside each request. This integration has been exercised on CPython 3.12 and 3.14; other interpreter versions need their own qualification.

## Run two unchanged applications

Install the [Python adapter dependencies](python.md), build the CLI and start the [Collector, Tempo and Grafana stack](observability-stack.md):

```sh
make setup build stack-up
./target/debug/quux-otelc --config examples/python-http-client.toml \
  --language python doctor
```

In one terminal, start the server. It prints `ready`, serves one request and exits normally:

```sh
OTELC_REPORT_PATH=/tmp/python-http-server.json \
  ./target/debug/quux-otelc --config examples/python-http-server.toml \
  python examples/apps/python_http_server.py --port 8080
```

In another terminal, run the client:

```sh
OTELC_REPORT_PATH=/tmp/python-http-client.json \
  ./target/debug/quux-otelc --config examples/python-http-client.toml \
  python examples/apps/python_http_client.py --port 8080
```

The client prints `{"body": {"result": 42}, "status": 200}`. Both [server](../examples/apps/python_http_server.py) and [client](../examples/apps/python_http_client.py) contain ordinary standard-library code. Run the same files with `.venv/bin/python` instead of the instrumentation command to obtain the baseline. Restart the one-request server before each client run. Add `--path /error` to exercise a caught business exception and HTTP 503; the original response body and successful process exit remain the same.

Open the [trace dashboard](http://localhost:3000/d/otelc-traces). Select service `otelc-python-http-client` and open its trace. The selected trace includes both service identities and this parent chain:

```text
request_order                 otelc-python-http-client  INTERNAL
  HTTP client                 otelc-python-http-client  CLIENT
    HTTP server               otelc-python-http-server  SERVER
      Handler.do_GET          otelc-python-http-server  INTERNAL
        process_order         otelc-python-http-server  INTERNAL
```

Allow for Collector batching and Tempo ingestion. An exporter acknowledgement does not prove storage. Check each runtime report for zero loss, `export_finished=true` and no active/queued trace trees. Two processes produce two completed local trees sharing one distributed trace ID; local buffering and limits do not make export across services transactional.

## Configuration

The [client](../examples/python-http-client.toml) and [server](../examples/python-http-server.toml) use the common schema-2 policy:

```toml
[traces]
enabled = true
root_sample_ratio = 1.0

[propagation]
http = true

[adapters.python]
backend = "profile"
http = "stdlib"
```

`http = "stdlib"` is the default integration when Python HTTP propagation is enabled. Unknown integration names fail validation. Other language adapters continue to report HTTP propagation as unavailable. Source/function patterns select business-function spans; opted-in HTTP boundary spans do not require selecting library source. `propagation.tasks` separately enables the [qualified task/worker integrations](python-task-context.md).

## Boundaries and limitations

- Clients: exact `http.client.HTTPConnection` and `HTTPSConnection` with the standard `HTTPResponse`, including standard `urllib.request` paths using them. Request and low-level header APIs, HTTP/1.1 reuse, body reads, EOF/observed close and certificate rejection have focused tests. A CLIENT span ends at body completion or observed close, rather than response headers. Unconsumed requests at shutdown are censored. Custom connection/response implementations need separate qualification.
- Servers: standard `BaseHTTPRequestHandler` dispatch with ordinary parser-owned header storage. The SERVER span starts after successful request-header parsing and ends when `handle_one_request` returns. It excludes header-receive time; malformed requests rejected before dispatch do not produce a completed SERVER span. Unsupported custom storage is suppressed with a loss rather than inspected through application getters. HTTP errors and the server's internally handled timeout are observable; caught application errors do not become invented escaping exceptions.
- Existing application trace headers are preserved, including invalid or duplicate headers. A valid explicit outgoing context is linked to the CLIENT span rather than overwritten. Invalid incoming parents start independent roots; invalid state is dropped independently. Unsampled valid parents retain their wire identity and sampling decision without exporting sampled spans. The [bounded carrier contract](trace-context-carrier.md) defines parsing and size limits.
- The private SDK provider/context does not replace an application's global provider, automatically adopt arbitrary manually created SDK spans or claim compatibility with other libraries patching the same methods. ASGI/WSGI, requests-specific hooks, async clients, HTTP/2, browser clients and mixed-language service graphs remain separate qualification work.
- Trace and metric exporter traffic is excluded using owned transport context and configured sink identities. Other application routes, including the same path at another authority, remain instrumented.
- Pending HTTP work shares bounded trace/active-call accounting with selected functions. Weak metadata does not add fields to connections, responses or handlers. Loss, incomplete shutdown and unsupported shapes are reported; no request payload, exception message or invented completion time is recorded. Live metrics controls do not disable configured traces.

`make python-check` runs real socket, TLS, transport, public CLI and failure-control tests, enforcing independent minimum 80% coverage for client, server, carrier and trace transport components. These correctness checks do not establish a production overhead budget or completion of the all-language tracing goal.
