# Python automatic task context

Enable `propagation.tasks=true` alongside `traces.enabled=true` in the common schema-2 configuration. Application files stay unchanged: no imports, annotations, decorators or manual context handoffs are required. The private monitoring adapter captures the selected function context at standard asyncio and ThreadPoolExecutor submission and restores it while selected coroutine bodies execute.

## Ordinary source and commands

The [ordinary example](../examples/apps/python_tasks_app.py) uses `TaskGroup`, `gather` and a detached child:

```python
async def leaf(value):
    await asyncio.sleep(0)
    return value + 1

async def detached(value):
    return asyncio.create_task(leaf(value))
```

The [external configuration](../examples/python-task-context.toml) selects `group`, `detached` and `leaf`, leaving `main` unselected. It adds:

```toml
[traces]
enabled = true
root_sample_ratio = 1.0

[propagation]
tasks = true
```

Install the [Python adapter dependencies](python.md), then run from the repository root:

```sh
make build
make stack-up
.venv/bin/python examples/apps/python_tasks_app.py
OTELC_PYTHON="$PWD/.venv/bin/python" ./target/debug/quux-otelc \
  --config examples/python-task-context.toml --language python doctor
OTELC_PYTHON="$PWD/.venv/bin/python" OTELC_REPORT_PATH=/tmp/python-tasks.json \
  ./target/debug/quux-otelc --config examples/python-task-context.toml \
  python examples/apps/python_tasks_app.py
```

Both executions print `results=23,43,31`. At full sampling, expect eight spans in three trees: two independent group roots with two children each, plus a detached root with one child. The detached child finishes after its parent; this is valid causal parenting. The parent's duration remains its actual function execution interval.

Open [Grafana traces](http://localhost:3000/d/otelc-traces), select service `otelc-python-task-context` and inspect the group and detached traces. Allow for Collector batching and Tempo ingestion. Reported export success alone does not prove storage; inspect the stored parent IDs and span counts.

## Qualified boundaries and losses

This supports CPython 3.12+ standard `asyncio.BaseEventLoop.create_task`, reached by `asyncio.create_task`, loop submission, `gather` and TaskGroup. Tests cover explicit empty/copied contexts, eager factories, compatible custom factories, cancellation before execution, escaping/caught cancellation, new submission after cancellation, failed submission, parallel roots and detached children. An explicit context with no otelc parent deliberately creates an independent root, including a copied snapshot that contains no admitted parent. Eager execution respects that isolation despite a physical caller still being on the stack. Default submission from a rejected selected creator stays suppressed when capacity later recovers. The application's original coroutine, factory, result and exception behaviour are preserved. One otelc task-context hook may be active per process; a second installation fails before replacing it.

The adapter uses a private ContextVar and bounded trace identities. It retains no application frames, tasks, futures or payloads. Pending submission reservations keep the local tree available until scheduled work completes; they do not keep the parent span open or wait for children in application code. Each reservation is released after completion or failed submission, including cancellation before start. `runtime.max_active_calls` bounds reservations separately from active function observations.

The report's `traces.pending_contexts` shows outstanding reservations. `context_capacity` invalidates an affected tree when the reservation pool fills. `context_hook` reports an incompatible factory result that cannot accept completion observation while preserving its original result. Selection/active/span limits invalidate the inherited tree, including already completed siblings. Incomplete trees are discarded at shutdown; no fake completion is exported. Sampling decisions are inherited, including zero sampling. Metrics live controls retain their existing semantics and do not disable configured tracing.

Direct detached `asyncio.Task(...)` construction, event loops overriding the standard method, direct thread creation, process/alternative executor handoffs, previously saved submission aliases, arbitrary callback scheduling and HTTP propagation are not qualified. An inherited sampled identity whose tree has already expired is suppressed and reports `context_expired`; it is not silently resampled as a new root. This diagnostic cannot detect every unqualified scheduler. Keep propagation disabled for unsupported execution models until their integrations are tested. Existing application SDK contexts are not automatically joined.

## Thread-pool workers

The same policy also qualifies standard `concurrent.futures.ThreadPoolExecutor.submit`, `asyncio.to_thread` and the standard loop's `run_in_executor` with a thread pool. Submission captures only the private otelc parent; application ContextVars retain their original semantics (`to_thread` copies them, ordinary pool submission does not). Worker results and exception instances are preserved. Completion, cancellation before start and failed submission release bounded reservations. Reused workers restore their previous private context. The runtime holds no strong references to Futures, callables or payloads.

The [unchanged worker example](../examples/apps/python_workers_app.py) uses ordinary pool submission:

```python
def root(pool, value, gate=None):
    return pool.submit(child, value, gate)

async def async_root():
    return await asyncio.to_thread(child, 40)
```

Run the plain application and the externally selected variant:

```sh
.venv/bin/python examples/apps/python_workers_app.py
OTELC_PYTHON="$PWD/.venv/bin/python" OTELC_REPORT_PATH=/tmp/python-workers.json \
  ./target/debug/quux-otelc --config examples/python-worker-context.toml \
  python examples/apps/python_workers_app.py
```

Both print `results=11,21,31,41; original-error=True`. At full sampling, expect ten spans in five independent trees, each root with one worker child, and one escaping worker-error span. No additional await or join is injected. Direct `threading.Thread.start`, ProcessPoolExecutor, custom submit overrides and saved aliases bypassing the installed hook remain unqualified. Pending work at shutdown remains incomplete; no successful child completion is invented.

## Verification and next steps

`make python-check` runs task regressions and a real CLI/OTLP decoder test that compares unchanged source bytes, stdout, stderr and status, checks eight spans/three trees and causal parents, and checks zero trace losses and pending reservations. It enforces an independent minimum 80% line-coverage gate for the task-context and worker-context implementations, alongside existing Python/trace gates. The previous default of independent scheduled roots remains covered with propagation disabled.

This milestone delivers task context, not automatic object/resource lifetime spans or cross-service propagation. Those follow as separate PRs under the [all-language implementation plan](context-and-lifetime-plan.md).
