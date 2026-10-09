# Go function spans

The schema-2 trace policy enables Go spans through private compiler overlays. Original `.go`, `go.mod` and `go.sum` files remain unchanged. Existing comments are optional; configuration can select ordinary functions, methods, generic functions and callbacks without SDK imports.

## Run and view

Start the [Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build go-check
docker compose up -d
go run examples/apps/go_app.go
./target/debug/quux-otelc --config examples/go-traces.toml --language go doctor
OTELC_REPORT_PATH=/tmp/go-traces.json \
  ./target/debug/quux-otelc --config examples/go-traces.toml \
  go examples/apps/go_app.go
```

Both executions print `72`. The unchanged example produces ten selected invocations in seven trees: recursion, independent goroutine calls, a method, local panic recovery and an escaping original panic. Use **Service** `otelc-go-traces` in the [trace dashboard](http://localhost:3000/d/otelc-traces). Check stored parent links and error status; an export acknowledgement alone does not prove Tempo storage.

Run the annotated example with the same policy:

```sh
./target/debug/quux-otelc --config examples/go-traces.toml \
  go examples/apps/go_annotated.go
```

It prints `30` and emits two independent spans. The existing `otelc.instrument` comment selects `selected`; configuration selects `configured`; `otelc.exclude` prevents `excluded` from receiving a probe. See the [Go 101 guide](go.md#optional-comments-and-generated-input).

With tracing enabled, generated compiler input follows this pattern:

```go
func selected(value int) int {
    defer __quux_otelc.FinishTrace(
        __quux_otelc.StartTrace("examples.apps.go_annotated.selected"))
    return value * 3
}
```

The binding is unique and private. The original function is not wrapped. Existing defers run before this probe, preserving named return updates and direct recovery. An escaping panic is rethrown with the original value; its span receives a generic error status without the panic value or application arguments. Timing includes deferred cleanup. `runtime.Goexit` runs defers and records completion, without claiming normal return.

## Parenting and bounds

Selected activations in the same goroutine use a private context stack; recursion and deferred selected cleanup are children of the admitted caller. Each goroutine starts its own root. No application context argument, global SDK provider, task propagation or distributed/manual SDK parent is added.

Goroutine identity comes from a bounded 64-byte `runtime.Stack` header read, qualified on Go 1.27.2. The header format is diagnostic, not a stable Go API. Unknown, truncated, zero or overflowing identities discard all active trees conservatively and expose `context_identity` loss. The application continues. This avoids exporting a partial tree when the parent cannot be identified. Header inspection adds per-call work; production overhead has no acceptance threshold yet.

SDK-generated IDs and parent-based root sampling apply to the entire tree. Only complete trees enter the queue. Active roots, spans per tree, pending calls, registered function names and queued batches are bounded. Total configured retained span capacity is at most 1,048,576, with one bounded tree in export. A rejected descendant discards its whole tree and releases retained SDK payload. No rejected child becomes a new root.

If a rejected root cannot enter the bounded goroutine map, a scalar suppression count prevents new independent roots until that activation exits. This can suppress otherwise admissible concurrent roots during overload; it avoids retaining an unbounded map of rejected goroutines. Loss and recovery are tested. Previously admitted trees still finish unless their own descendants are rejected.

## Export, controls and limitations

The private pinned Go SDK encodes protobuf once. A serial trace phase uses independently resolved trace endpoint, protocol, timeout and runtime-only headers. It allows three attempts for transient transport/429/502/503/504 failures within one deadline. HTTP 200 and a decoded full-success acknowledgement are required. Redirects, permanent statuses, malformed/partial acknowledgements, requests over 16 MiB and responses over 64 KiB fail visibly; rejected response bodies are closed immediately. Completed failed trees are discarded with an export-loss counter, rather than replayed on the next metric interval.

Trace and metric transport are independent. Trace batches do not inherit the metric reader interval as their deadline. Shutdown shares one total budget, cancels an existing long trace phase, releases remaining queued trees and publishes final trace failure health while time remains. Expiry leaves `export_finished=false`. Forced process termination cannot guarantee final export.

Live `enable` and `disable` controls change metric admission only. Previously admitted metrics finish; trace production remains enabled according to the launch policy. There is no live trace-policy reload. See [latency comparisons](go.md#live-controls-and-latency).

The [existing Go build restrictions](go.md#build-and-runtime-boundaries) still apply. Legacy `GODEBUG=panicnil=1` is rejected at launch; a later dynamic change preserves panic propagation and discards the affected trace with visible loss. Workspaces, project cgo, vendor mode, task/distributed parenting, links and automatic lifetimes remain unsupported. Compiler overlays and SDK dependency resolution can change private build dependencies; retain normal application regression checks.

`make go-check` enforces formatting, vet, Staticcheck, vulnerability analysis, race detection and an 80% total product coverage floor. The new trace store, scope and transport each have a separate 80% statement-coverage gate. End-to-end tests decode actual SDK protobuf, compare unchanged and annotated application output and verify original source/module files.
