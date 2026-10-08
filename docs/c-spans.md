# C function spans

The LLVM adapter exports sampled function spans from unchanged C applications. The current qualification target is macOS ARM64, Clang/LLVM 22 and the repository's locked Rust toolchain. Callback instrumentation, C++, shared libraries, LTO and sanitizers are not qualified by this implementation. Unsupported combinations fail visibly.

## Run an unchanged application

Build the CLI, native runtime and matching LLVM plugin, then start the documented Collector, Prometheus, Tempo and Grafana stack:

```sh
make build
make stack-up
mkdir -p build/tutorial
./target/debug/quux-otelc --config examples/c-traces.toml --language c doctor
./target/debug/quux-otelc --config examples/c-traces.toml --language c clang \
  -O1 -g examples/apps/c-traces.c -o build/tutorial/c-traces
OTELC_REPORT_PATH=build/tutorial/c-traces-report.json \
  ./target/debug/quux-otelc --config examples/c-traces.toml --language c run \
  build/tutorial/c-traces
```

The source in `examples/apps/c-traces.c` has ordinary functions:

```c
int child_order(int value) { return value + 1; }
int parent_order(int value) { return child_order(value); }
int recursive_order(int depth) {
    return depth ? recursive_order(depth - 1) + 1 : 0;
}
```

Configuration selects them; the compiler adds probes to its private intermediate representation. Application and dependency source files remain unchanged. The complete example preserves `errno` and returns `result=8`. It produces seven spans in three trees: a parent/child pair, four recursive invocations and an independent child call on another thread.

## Read existing annotations

The same configuration reads the existing annotations in `examples/apps/annotated.c`:

```c
__attribute__((annotate("otelc.instrument")))
int process_order(int value) { return value * 2; }

__attribute__((annotate("otelc.exclude")))
int audit_order(int value) { return value + 1; }
```

```sh
./target/debug/quux-otelc --config examples/c-traces.toml --language c clang \
  -O1 -g examples/apps/annotated.c -o build/tutorial/c-annotated-traces
./target/debug/quux-otelc --config examples/c-traces.toml --language c run \
  build/tutorial/c-annotated-traces
```

Explicit configuration exclusions take precedence. The example records `process_order` and the configured `configured_order`; annotation exclusions and unrelated vendor annotations do not select other functions. No annotation insertion into original source is required.

## Parenting, buffering and limits

The nearest selected invocation on the same thread is the parent, including calls through unselected functions. A newly created thread starts independent roots. Recursion gets distinct SDK span IDs. The OpenTelemetry SDK supplies IDs, root sampling and span data; its protobuf conversion supplies OTLP encoding. Private instrumentation does not adopt or change the application's global tracer provider.

Probes use fixed producer slots, stack frames and queues. They do not allocate, lock a mutex, construct SDK objects or perform network work. A root emits one additional primitive entry record; exits carry native parent tokens, completion counts and monotonic times. Each root captures its producer wall epoch. Queue processing delay does not become span duration. Initial thread registration is a cold path; runtime lifecycle and telemetry threads suppress their own probes, including application allocator callbacks.

`runtime.max_active_calls`, native stack/thread/queue limits and candidate-root admission bound producer work. `traces.max_active_traces` also bounds SDK roots waiting for completion. Admission can be conservative when workers lag. SDK sampling occurs on the worker; reducing the sample ratio does not remove native probe and queue costs.

The worker waits for the root's completion before constructing children. Missing records, invalid ordering, non-local exits, slot retirement and capacity losses discard the affected tree. Rejected outer scopes suppress descendant roots until the original scope exits, including after capacity recovers. SDK active and queued trees plus pending primitive records have fixed record-count limits; one root under construction and one popped SDK/protobuf export can coexist. `max_spans_per_trace * max_active_traces` is capped at 1,048,576, export queues at 64 trees, names at 1 KiB, requests at 16 MiB and acknowledgements at 64 KiB. The native pool byte estimate excludes additional bounded SDK/worker allocations and thread stacks.

Inspect both the report's native `losses` and `traces.losses`. Producer rejection can occur before an SDK tree exists and is recorded in native losses. `otelc.trace.dropped_trees` exposes SDK/worker whole-tree losses; `otelc.export.dropped_batches` covers failed metric or trace exports. `completed_trees` means local completion, not confirmed downstream delivery. Partial Collector acceptance cannot be rolled back. Exhausted exports are discarded without automatic replay.

## Export, controls and shutdown

Metrics and traces have separate workers and independent endpoints, headers and timeouts. Trace settings use `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_HEADERS` and `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`, with the common OTLP variables as fallbacks. Headers are read at runtime and stay out of the manifest. Only HTTP 200 with a valid trace acknowledgement succeeds; permanent rejection, malformed/partial acknowledgements and exhausted retries are visible losses. Transient requests have at most three attempts within one phase budget.

Function metrics can be disabled initially or changed live with the LLVM control socket; trace collection continues independently. A function retains its metric admission decision until exit. `traces.enabled` and sampling are static configuration for this process, so metrics controls do not turn traces off.

Shutdown shares one caller budget across drain, traces, control and metrics. An HTTP request or SDK allocation already in progress can outlive that wait; the process reports `export_finished=false` rather than claiming delivery. The implementation does not interrupt an already-started HTTP request. A contended optional trace snapshot is `null`, and unfinished aggregation can leave `drained=false`. These are incomplete observations. Health changes remain exportable while function metrics are disabled; final health delivery is not guaranteed after the shutdown deadline expires.

Calls before native initialisation or after shutdown are omitted. Signal handlers, cross-thread task parenting, distributed context propagation, manual SDK parenting, links and automatic object/resource lifetimes remain unsupported. These spans contain names and timing, without arguments, return values or application error messages.

## View and compare

Open [the trace dashboard](http://localhost:3000/d/otelc-traces) and select service `otelc-c-traces`. Copy a trace ID from Tempo search into the dashboard's Trace ID field. Metrics are available in [the instrumentation dashboard](http://localhost:3000/d/otelc-local/otelc-instrumentation). See [the stack guide](observability-stack.md) for endpoints, retention and shutdown instructions.

Use the existing [benchmark guide](benchmarks.md) to compare plain, probes-disabled and metrics-enabled native builds and same-process metrics off/on runs. Those metrics-only results do not measure enabled tracing. Keep tracing fixed in both sides of a live metrics comparison; measure tracing overhead separately against the same workload and toolchain. No production overhead threshold or stable performance claim is made here.
