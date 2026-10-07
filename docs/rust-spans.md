# Rust function spans

Rust can export sampled function spans as well as cumulative metrics using the common schema-2 policy. The source adapter inserts probes into private compiler inputs. Application and dependency source, manifests and lockfiles remain unchanged; no SDK import or annotation is required.

## Run an unchanged application

Start the [local Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build
docker compose up -d
./target/debug/quux-otelc --config examples/rust-traces.toml --language rust doctor
rustc --edition=2024 examples/apps/rust_trace_app.rs -o /tmp/plain-trace-app
/tmp/plain-trace-app
OTELC_REPORT_PATH=/tmp/rust-trace-report.json \
  ./target/debug/quux-otelc --config examples/rust-traces.toml \
  rust examples/apps/rust_trace_app.rs
```

Both applications print `trace results preserved`. The unchanged example contains ordinary recursive functions and directly awaited async functions:

```rust
fn recursive(depth: u32) -> u32 {
    if depth == 0 { 0 } else { 1 + recursive(depth - 1) }
}
async fn child() -> u32 { YieldOnce(false).await; 42 }
async fn parent() -> u32 { child().await }
```

The [full example](../examples/apps/rust_trace_app.rs) also moves a polled future to another thread, cancels a pending future and checks the original panic payload. Configuration selects eight invocations forming four trees: four recursive spans, two async spans, one cancelled span and one escaping-panic span. Selection exclusions and optional existing annotations work exactly as for [Rust timing](rust.md).

```toml
schema_version = 2
languages = ["rust"]
[sources]
include = ["examples/apps/rust_trace_app.rs"]
[functions]
include = ["*.recursive", "*.parent", "*.child", "*.cancelled", "*.escaping"]
[traces]
enabled = true
root_sample_ratio = 1.0
max_active_traces = 32
max_spans_per_trace = 256
[export]
endpoint = "http://127.0.0.1:4318"
interval_ms = 500
```

Open the [read-only trace dashboard](http://localhost:3000/d/otelc-traces). Its Service field defaults to `otelc-rust-traces`. Click a trace name in the search table, or paste a known ID into the Trace ID field, to inspect its nested spans. The anonymous Viewer role does not permit Explore, so this dashboard provides the supported viewing path. Tempo search and query ingestion can lag the application report; a successful app export means Collector acceptance, not proof that Tempo persisted the trace.

## Parenting, sampling and failure

The Rust OpenTelemetry SDK supplies random IDs, root sampling and span data; upstream protobuf conversion supplies OTLP encoding. The adapter buffers complete local trees to keep parents and children together. Root sampling is inherited by descendants. Unsampled calls are not counted as trace loss, and metrics continue independently of sampling.

Synchronous calls inherit the active context on their thread. An async function acquires its context at first poll, attaches it only during each poll and cleanup, and restores the previous thread context afterwards. This preserves `Send` where the original future is `Send`, supports thread migration and leaves non-`Send` futures usable on their original executor. Unpolled futures produce no spans. Cancellation closes the admitted span with an error status and `otelc.cancelled=true`; escaping unwinds get an error status without capturing panic payloads. A panic caught inside a function leaves that function's span status unset.

Each root anchors wall time once; subsequent timestamps use monotonic elapsed time. Clock adjustments during a tree do not reorder its timestamps. Independently scheduled tasks start new roots unless polled within a supported active parent. Automatic executor task propagation, cross-process propagation, remote parents, span links and manual SDK context interoperability are not implemented.

The shared function and active-call limits still apply. Exceeding those limits or a span limit invalidates the affected whole tree, including completed siblings. Incomplete trees are discarded at shutdown. Completed trees queue up to `export.max_queued_batches`; queue overflow discards a whole tree. Active trace and per-trace span capacities have a combined maximum of 1,048,576 records; queued records share that hard record bound. Trees exceeding a conservative 16 MiB payload budget are discarded before duplicating their names into SDK/protobuf structures. Names are limited to 1 KiB and contain no application arguments or object addresses.

The report's `traces` section exposes completed, active, queued and sampled-out counts and loss reasons. `otelc.trace.dropped_trees` exposes discarded-tree reasons as metrics. A completed-tree count means local completion, not confirmed delivery. Export rejection, exhausted transient retries, malformed/partial acknowledgements or elapsed shutdown deadlines increase `export_loss`; inspect `export_finished` too. Partial Collector acceptance cannot be rolled back, so whole-tree buffering does not guarantee a transactional downstream write. No automatic replay after an exhausted export is promised.

## Independent metrics and trace settings

Traces remain enabled or disabled by launch configuration. Live `enable`/`disable` commands control **metrics admission**; they do not toggle tracing. Setting `[metrics] enabled=false` still permits traces, and disabling metrics preserves spans for already configured tracing.

The generic OTLP endpoint is a base URL, with `/v1/metrics` and `/v1/traces` appended. Signal-specific endpoints are complete URLs. Trace environment variables override generic ones and then configuration:

- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` (only `http/protobuf`)
- `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` (milliseconds, 1–60000)
- `OTEL_EXPORTER_OTLP_TRACES_HEADERS`

Headers fall back to `OTEL_EXPORTER_OTLP_HEADERS` and are read at launch without being saved in plans or reports. Metrics-specific endpoint, protocol, timeout and header values do not change trace settings. Both signals share the configured shutdown deadline. Trace-only settings are ignored when tracing is disabled. Remote endpoints require HTTPS; loopback development endpoints may use HTTP.

Tracing adds ID generation, context handling, buffering, locking and export costs. Metrics-only off/on benchmarks do not measure that cost. No production overhead threshold or stable performance claim is made here. This backend is qualified for Rust; enabling traces in other language adapters continues to fail visibly until their individual implementations are delivered.
