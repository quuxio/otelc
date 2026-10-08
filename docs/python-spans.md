# Python function spans

CPython 3.12+ monitoring can produce function spans from unchanged application code using the common schema-2 policy. No source imports, decorators, wrappers or rewritten code objects are required. The adapter uses a private OpenTelemetry tracer provider and the upstream SDK exporter, without replacing an application's global tracer provider.

## Run and view

Start the [local Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build
docker compose up -d
python3.12 examples/apps/python_trace_app.py
./target/debug/quux-otelc --config examples/python-traces.toml --language python doctor
OTELC_REPORT_PATH=/tmp/python-traces.json \
  ./target/debug/quux-otelc --config examples/python-traces.toml \
  python examples/apps/python_trace_app.py
```

Both runs print `trace results preserved`. The [ordinary example](../examples/apps/python_trace_app.py) contains recursion, directly awaited coroutines, cancellation, a generator and an escaping exception whose original object is preserved:

```python
def recursive(depth):
    return 0 if depth == 0 else 1 + recursive(depth - 1)

async def child():
    await asyncio.sleep(0)
    return 42

async def parent():
    return await child()
```

The [external configuration](../examples/python-traces.toml) selects nine invocations forming five trace trees. Existing optional comment annotations and exclusion priority work as described in the [Python metrics guide](python.md). Set `traces.enabled=true` and configure root sampling, active-tree and per-tree span limits in the shared policy.

Open the [read-only trace dashboard](http://localhost:3000/d/otelc-traces), change **Service** to `otelc-python-traces` and click a trace name to view its spans. Allow for Collector batching and Tempo ingestion. Export acknowledgement alone is not proof of downstream storage.

## Qualified behaviour and limits

The monitoring backend observes the original interpreter frames. Parenting follows the nearest selected active caller, including directly awaited coroutines and generator calls. Suspended frames remain outstanding until completion or unwind; original application frames and argument values are never retained by the span store. Code identities remain in the existing bounded selection cache. Parent search is bounded to 4096 caller links and reports a trace loss if exceeded.

Spans start when the function first executes and end on its original return or unwind. An unstarted coroutine produces no span. Coroutine cancellation and generator close receive error status and `otelc.cancelled=true`; escaping exceptions receive error status without recording exception objects, messages or arguments. Caught exceptions leave the enclosing span's status unset. Selected generators can move between threads while keeping their original trace identity. Independently scheduled tasks start new roots by default. With `propagation.tasks=true`, qualified standard asyncio tasks inherit their submission context; see [Python task context](python-task-context.md). Thread/executor and distributed propagation, remote or manual SDK parenting and span links remain unavailable.

SDK root sampling is inherited by descendants. Function, active-call, active-tree, per-tree span and queue limits are bounded. Rejection invalidates an affected whole tree, including completed siblings. Invalidated payloads are released while existing calls finish. Incomplete trees are discarded at shutdown. Active and queued records share a maximum of 1,048,576 spans, with one bounded tree at a time passed to the exporter. Trees beyond a conservative 16 MiB byte budget are discarded before encoding; the SDK also limits encoded requests to 16 MiB. Complete local buffering does not provide transactional downstream writes: partial Collector acceptance cannot be rolled back.

The report's `traces` section and `otelc.trace.dropped_trees` metric expose loss. Failed export, rejected or malformed acknowledgements and exhausted retries increase `export_loss`. An exhausted trace export is discarded rather than replayed automatically. Check `export_finished`: a shutdown timeout can leave an in-flight daemon export unfinished even though application shutdown returns.

Trace endpoint, headers, protocol and timeout use the same independent signal precedence as [Rust trace export](rust-spans.md#independent-metrics-and-trace-settings). Empty trace-specific headers suppress generic headers. Live `enable`/`disable` controls metrics admission only; traces continue when configured, and metric admission stays fixed for each existing call. Tracing adds interpreter, SDK, buffering and export costs; metrics-only benchmarks do not measure tracing overhead, and no production overhead threshold is claimed.
