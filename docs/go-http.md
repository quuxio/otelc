# Go HTTP context without source edits

<!-- markdownlint-disable MD013 -->

This candidate applies the common `[context] http = true` switch to ordinary `net/http.Transport` clients and the standard server handler entry. It uses the pinned Go compiler, the existing official context bridge and [OpenTelemetry's `otelhttp` wrapper](https://pkg.go.dev/go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp@v0.72.0). Developers add no imports, middleware, annotations or telemetry bootstrap. Go remains a candidate pending independent provider qualification and final delivery gates.

## Build and run the ordinary example

The same [HTTP policy](../examples/http-context.toml) selects the ordinary Java, JavaScript, Microsoft TypeScript, Python and Go HTTP examples. It starts metrics off while tracing remains enabled. The [Go application](../examples/go-http/main.go) uses only the standard library and supports server/client roles from one stock source module.

```sh
bin/quux-otelc --config examples/http-context.toml go-build \
  --root "$PWD/examples/go-http" --output "$PWD/build/go-http-example"
bin/quux-otelc --config examples/http-context.toml go \
  --build "$PWD/build/go-http-example" --runtime "$PWD/build/go-http-server" -- server
```

The server prints its ordinary URL. Keep that process open and use the URL in a second terminal:

```sh
bin/quux-otelc --config examples/http-context.toml go \
  --build "$PWD/build/go-http-example" --runtime "$PWD/build/go-http-client" \
  -- client http://127.0.0.1:REPLACE_WITH_PRINTED_PORT
```

Enter `normal`, `child`, `reuse`, `error`, `cancel` or `panic` in the client. `reuse` makes a second request with the context exposed by the first response; both operations retain their own spans. Enter `quit` to close each process. `leak` deliberately retains an unread response body: the application still returns normally, but its final instrumentation status fails with an active HTTP span. Use fresh build/runtime directories. Add `--plain` to launch the stock binary. The `child` command spawns and joins an ordinary goroutine on the server; `[context] tasks` controls its inheritance independently of HTTP propagation.

The shared live commands work in another terminal:

```sh
bin/quux-otelc metrics --socket "$PWD/build/go-http-client/control.json" on
bin/quux-otelc metrics --socket "$PWD/build/go-http-server/control.json" on
bin/quux-otelc health --socket "$PWD/build/go-http-client/control.json"
```

`collection` and `emission` switch independently in the same PID. Both function metrics and the official HTTP metric scope use the owned gates; HTTP spans and W3C propagation remain active while metrics are off. Disabled trace recording uses a non-recording owned provider for context, with no trace exporter or invented HTTP recording counts. Configuration 2, build 3 and health 4 are explicit in `go-protocols`; earlier build/runtime contracts are refused before launch.

## Reuse and preservation

The compiler injects a guarded native entry, then the official wrapper calls the original method through a separately marked second entry. The second entry consumes its single-use atomic token before application callbacks run. Returned and concurrently reused contexts cannot retain suppression; wrapper return also invalidates an unused token. Direct injection preserves original panics: a recoverable instrumentation before-hook must not catch a panic from an original method called inside it. Client/server tokens are distinct, so an incoming handler can make outgoing requests with its inherited context. SDK export traffic has a private context marker; arbitrary application headers or User-Agent text cannot suppress tracing.

The wrapper uses explicit owned tracer/meter providers and W3C trace-context/baggage propagation. It never replaces application globals. Client requests are cloned before header injection. The official wrapper preserves supported response-writer interfaces, informational/final header forwarding and upgraded response-body writes. A generated dependency initialises the owned runtime before original main-module package variables; original source files remain unchanged.

`go_http_bundle` verifies the official module version, Go checksums and exact archive identity, then materialises only required production source, module locks and licence. Its checked three-expression adaptation omits telemetry error descriptions that would otherwise call the application's `Error()` method. Original error values are returned unchanged; the official concrete error-type attributes remain. The resulting source is held in the private build and covered by its input identities. The module is `otelhttp v0.72.0`, aligned with the owned SDK `v1.47.0`; no application dependency version may change to accommodate it.

Client operation spans follow the official EOF/close-attempt boundary. That is separate from a successful resource release or object GC. Health 4 counts actual recording-SDK HTTP start/end callbacks; an unread retained body remains incomplete. End calls after SDK shutdown do not fabricate an observed completion. Unsampled context is not misreported as a recorded span.

## TLS HTTP/2 example

The ordinary [HTTP/2 example](../examples/go-http2/main.go) uses the same policy, a standard TLS server and a client that trusts that server's certificate explicitly. It makes one request or four concurrent requests on the HTTP/2 transport. It adds no telemetry code and does not disable certificate verification.

Build it with `go-build --root "$PWD/examples/go-http2"` and fresh output/runtime paths as above. Run its `server` role first. Pass the complete printed JSON object as the client's single argument after `client`, for example `-- client '{"url":"https://127.0.0.1:PORT","certificate":"PRINTED_BASE64_CERTIFICATE"}'`. Enter `single` or `concurrent`, then `quit`. Each reply reports the actual protocol; the acceptance test requires `HTTP/2.0` in stock and instrumented lanes. The common policy starts metrics off; use the same live control commands to enable them.

## Checks and limits

```sh
make go-check
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_go_http_integration test_go_http_bundle test_go_health test_prepared_go
PYTHONPATH=tests .venv/bin/python -m unittest -v test_go_http2
```

The component checks use the actual pinned official wrapper, vet/race tests and per-file 80% floors. Compiler checks use ordinary native services and the public prepared launcher, covering all initial/live metric flags, W3C parentage, original header preservation, body abandonment, native error identity, formatter counts, panic/cancellation and task inheritance. SDK health and decoded receipt remain separate evidence.

The HTTP/2 test checks real TLS, single and concurrent requests, separate stream contexts, unchanged original headers/results, native parentage, clean final counts and startup-off to same-PID metric enable. Broader HTTP/2 cancellation, push, upgrades and flow-control behaviours still need qualification. Custom RoundTrippers that do not reach the standard transport, arbitrary external dependency initialisers, application-owned SDK interactions and complete source/dependency closure require separate work. The HTTP candidate does not establish generated oteleq workflow admission or a complete Go release. See the [source profile](go-source.md) and [local metrics/traces stack](../README.md#java-example-and-grafana).
