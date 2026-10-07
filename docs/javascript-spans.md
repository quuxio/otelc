# JavaScript function spans

The external Node loader can export sampled function spans under the common schema-2 policy. Original application and dependency files remain unchanged. The adapter uses a private OpenTelemetry tracer provider and explicit contexts; it does not replace the application's global provider.

## Run and view

Start the [Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build node-check
docker compose up -d
node examples/apps/javascript_trace_app.mjs
./target/debug/quux-otelc --config examples/javascript-traces.toml --language javascript doctor
OTELC_REPORT_PATH=/tmp/javascript-traces.json \
  ./target/debug/quux-otelc --config examples/javascript-traces.toml \
  node examples/apps/javascript_trace_app.mjs
```

Both runs print `trace results preserved`. The [unedited example](../examples/apps/javascript_trace_app.mjs) records eleven invocations in five trace trees: recursion, synchronous parenting, direct async parenting, an async bare Promise return and preserved exception identity.

```js
export function child() { return 42; }
export async function asyncChild() { await Promise.resolve(); return child(); }
export async function asyncParent() { return asyncChild(); }
```

The [external policy](../examples/javascript-traces.toml) selects the functions. Existing optional `otelc.instrument` and `otelc.exclude` comments still follow the [JavaScript 101 guide](javascript.md); no application SDK import or manual span is needed. ESM and CommonJS share the backend.

Open the [read-only trace dashboard](http://localhost:3000/d/otelc-traces), set **Service** to `otelc-javascript-traces`, then select a trace. Collector acknowledgement does not prove downstream storage; allow for batching and inspect Tempo through the dashboard.

## Behaviour and boundaries

Synchronous spans cover the selected body until return or an escaping throw. An `async` function's span ends at its original outer Promise settlement, including a bare returned Promise. The V8 observer adds no Promise reactions, extra awaits, then-property reads or rejection handlers. The in-memory transform detaches private context before each original await or ordinary yield and restores it on resumption, catch binding and finally. Nested await operands are handled individually. Parent links follow direct calls and directly awaited selected functions; independent callbacks/tasks start roots. A regular function returning a Promise ends at synchronous return.

Parameters, returned objects and exception messages are not recorded by the trace store. The existing observer briefly retains a settled Promise until its state can be read; that can extend value/rejection lifetime. Timing begins after parameter initialisation. Generator spans start on first execution and end on return or throw; abandonment is incomplete, and explicit generator return is normal completion. Selected async generators, delegated yields, `for await` and `with` fail visibly with tracing until separately qualified. Dynamic `with` lookup could intercept inserted helper bindings. The existing loader, direct-eval, worker, dependency-selection and raw V8 origin boundaries still apply. Missing/deep origins invalidate the affected trace with visible loss.

Root sampling uses SDK IDs and is inherited. Function, active-call and per-tree span rejection discards the affected whole tree and suppresses descendants. With traces enabled the function cache admits calls when invoked; the separate async-origin table remains bounded by `max_functions`. Invalidated SDK payloads are released while admitted calls finish. Active and queued spans share a hard maximum of 1,048,576 records, with one additional bounded tree in flight. Completed-tree queue limits, a conservative 16 MiB pre-encoding budget and an encoded-size check bound export. Unfinished sampled trees are discarded at shutdown.

Trace endpoint, headers, protocol and timeout follow the common [independent signal precedence](rust-spans.md#independent-metrics-and-trace-settings). Empty trace-specific headers suppress generic headers. The metric reader triggers a separate trace phase; its interval does not shorten the trace deadline. At most three transient attempts reuse the same encoded body within that phase. Partial, malformed, oversized and permanent rejections are terminal. Rejected HTTP responses are closed immediately. Exhausted batches are discarded with `export_loss`; partial downstream acceptance cannot be rolled back. Loss counters appear in the next metric snapshot even without new application calls.

Shutdown waits for both signals within its shared budget. Trace transport finishes before the metric reader's final collection, so a rejected final trace batch can be included in the last health metric. Check `export_finished`: timeout means completion is unverified. Live `enable`/`disable` changes metric admission only; configured traces continue, and existing calls keep their original metric admission. Tracing adds SDK, context, buffering and transport cost. Metrics-only benchmarks do not qualify span overhead, and no production latency threshold is claimed. TypeScript spans require their own qualification PR.
