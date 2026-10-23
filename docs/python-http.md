# Source-free Python HTTP context

<!-- markdownlint-disable MD013 -->

The Python candidate applies the common `[context] http=true` switch to ordinary `urllib.request` clients and stdlib `wsgiref` servers. It uses the pinned official urllib instrumentor and WSGI middleware, with generated, source-bound preservation fixes. Developers add no telemetry imports, annotations or middleware bootstrap. Python remains unadmitted while full source evidence, independent review and oteleq qualification are outstanding.

The same [HTTP policy](../examples/http-context.toml) can drive the Java, JavaScript, Microsoft TypeScript and Python examples. Its function globs cover their ordinary method names; it contains no per-language settings. The HTTP switch separately enables supported library spans/metrics, including when application function selection excludes the source. Task propagation remains a separate switch. Both context switches are selected at launch; metric collection and emission change live in the same process without disabling traces.

## Run the ordinary application

[HttpContextApp.py](../examples/python/HttpContextApp.py) uses `Request`, `urlopen` and `make_server`. Its server returns `8` or a deliberate `500: bad` and retains a business request audit. Its client catches the normal `HTTPError` and reads the original response. The `/relay` route makes an ordinary downstream call.

Prepare the environment and [local Collector/Grafana stack](../README.md#java-example-and-grafana):

```sh
make setup
make stack-up
mkdir -p build/python-http
```

In terminal 1, start the server. Use a fresh runtime path for each run:

```sh
bin/quux-otelc --config examples/http-context.toml python \
  --root "$PWD" --entry examples/python/HttpContextApp.py \
  --runtime build/python-http/server -- \
  server build/python-http/requests.jsonl http://127.0.0.1:1 \
  | tee build/python-http/server.stdout
```

The ready line contains its chosen port and real application PID. The downstream address above is deliberately unused for `/ok` and `/error`; supply a running downstream address to use `/relay`.

In terminal 2, read that port and start an interactive client:

```sh
port="$(.venv/bin/python -c 'import json; from pathlib import Path; print(int(json.loads(Path("build/python-http/server.stdout").read_text().splitlines()[0])["port"]))')"
bin/quux-otelc --config examples/http-context.toml python \
  --root "$PWD" --entry examples/python/HttpContextApp.py \
  --runtime build/python-http/client -- client "http://127.0.0.1:$port" http://127.0.0.1:1
```

After the client prints its ready line, enable metrics for both apps in terminal 3:

```sh
bin/quux-otelc metrics --socket build/python-http/server/control.json on
bin/quux-otelc metrics --socket build/python-http/client/control.json on
```

Paste these lines in the client terminal:

```text
{"path":"/ok"}
{"path":"/error"}
```

The replies are `{"status":200,"body":"8","unchanged":true}` and `{"status":500,"body":"bad","unchanged":true}`, with normal JSON spacing. Both requests produce HTTP spans even when either metric switch is off. Use `off`, `collection on|off` or `emission on|off` with the same descriptors to compare live states. Type `quit` in each app terminal for a checked SDK drain. Add `--plain` before `--` for a native baseline without probes or an SDK process.

Open [function metrics](http://localhost:3001/d/otelc-lifetimes) and [task/service traces](http://localhost:3001/d/otelc-traces), choosing service `otelc-http-examples`. The server audit contains the received `HTTP_TRACEPARENT`; its second dash-separated field is the trace ID. Use it to select a trace. Prometheus receives `http_client_request_duration_seconds_count` and `http_server_request_duration_seconds_count`, grouped separately by language/resource/scope. Check actual receiver data; a successful export or clean SDK checkpoint alone is not downstream-receipt proof.

## Tooling and preservation boundary

The runtime generates private products from exact SHA-256 checked official `0.66b1` sources. It retains those product files and a source/product receipt under `http-products/`. The installed packages and application files are not rewritten. An unqualified source version fails startup before application execution.

The official packages still create HTTP spans, extract/inject W3C context and baggage, choose parents/sampling, construct semantic attributes and measure durations. Narrow fixes avoid formatting application exceptions, invoking custom exception attributes or `with_traceback` methods, asking `len` of file bodies, and turning malformed response length metadata into an application failure. Body-size metrics are omitted when size is unknown; zero is not invented. Invalid declared response lengths record observation loss and prevent a clean final report while preserving the native body.

Propagation fields reach a temporary HTTP transport header dictionary. Caller `Request` objects, header containers, order and values remain native during and after the call. The direct opener's native default timeout is retained. A context variable isolates concurrent calls. WSGI response iterables retain wsgiref's two close-attribute lookups, including getter side effects and the second lookup's callable. The close call runs once; span completion and context cleanup still run if either lookup or the call raises.

Library instruments use `python_meter_bridge` to send bounded primitives into the owned SDK process. The official SDK retains scope/version/schema, units/descriptions, histogram advice, attributes, aggregation and exemplars. Cached instruments read live collection state at each measurement. Emission and SDK retries remain outside the application. Collection filters API measurements: signed cumulative instruments describe accepted deltas rather than a complete global count across disabled intervals. The first profile supports synchronous instruments; requesting an observable callback records explicit unsupported loss without running it.

This profile covers ordinary synchronous urllib HTTP clients and stdlib WSGI servers. Existing OTel urllib owners are preserved and cause a startup refusal. Request subclasses are sent through the original opener without extra instrumentation queries, with explicit unsupported loss. Custom loaders/handlers/request hooks, other frameworks, asynchronous HTTP clients, process boundaries and complete import/code provenance are not yet qualified. Another tool's replacement patch is retained at shutdown and recorded as ownership loss. Raw API outcomes do not establish per-request downstream delivery.

## Checks

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_http_context test_python_http_components test_python_meter_bridge -v
make check
```

Native tests compare plain/instrumented output and sources, real app/SDK identities through four states, client/server parentage, remote sampled/unsampled parents, zero root sampling, baggage/tracestate and relays, tracing disabled and source exclusion. Component tests cover real transport calls, caller headers during concurrent requests, RNG preservation, exception identity, file bodies, unknown/malformed lengths, iterator cleanup, source guards and partial installation/shutdown failures. Explicit SDK calls in component fixtures are tests, not required developer code.
