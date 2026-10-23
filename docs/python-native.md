# Native Python frontend candidate

<!-- markdownlint-disable MD013 -->

This candidate adds selected function metrics/spans, weak object/resource lifetime observations, configured thread propagation and live metric controls to original CPython sources. It uses the official SDK tracer, context, sampling, threading instrumentor and metric/OTLP pipeline. The public `plan` still reports Python as remaining: broader library integration, complete source evidence, independent review and oteleq qualification are delivery work.

## An unchanged application

[BenchmarkApp.py](../examples/python/BenchmarkApp.py) is an ordinary application with no telemetry imports, decorators, annotations or bootstrap. Its workload uses the same seeded recurrence as the Java, JavaScript and TypeScript examples:

```python
def workload(seed):
    if seed < 0:
        raise ValueError('negative seed')
    item = WorkItem(seed)
    try:
        value = item.seed
        total = 0
        for _ in range(128):
            value = (value * 1664525 + 1013904223) & 65535
            total += value
        return total
    finally:
        item.close()
```

Use this function-only common policy to start with function telemetry. It contains no Python-specific settings and can drive the other language versions too. The full [example policy](../examples/otelc.toml) also selects `WorkItem` object/resource lifetimes.

```toml
schema_version = 3

[resource]
service_name = "otelc-examples"

[sources]
include = ["examples/**"]

[functions]
include = ["*workload*"]
exclude = ["*excluded*"]

[export]
endpoint = "http://127.0.0.1:4320"

[metrics]
collection = false
emission = false

[traces]
enabled = true
sample_ratio = 1.0

[lifetimes]
enabled = false
```

Save it as `/tmp/otelc-functions.toml`. From a prepared checkout, start the [local collection stack](../README.md#java-example-and-grafana), then launch the original source:

```sh
make stack-up
runtime="$(mktemp -d /tmp/otelc-python.XXXXXX)/runtime"
echo "$runtime"
.venv/bin/python -m otelc --config /tmp/otelc-functions.toml python \
  --root "$PWD" --entry examples/python/BenchmarkApp.py --runtime "$runtime"
```

Enter `10` for a batch of ten calls. Enter `throw` for a caught exception, or `quit` to finish. App stdout remains app output. The SDK drains in its own process after app execution. An unsuccessful drain returns failure when the app succeeded; an app failure keeps its original exit code.

In another terminal, use the printed runtime path from the first terminal:

```sh
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json status
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json collection on
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json emission on
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json flush
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json off
```

Use `.venv/bin/python` if `python` is not available. The endpoint is private authenticated loopback; the shared client reads its token without printing it. Collection and emission switch in the existing app/SDK pair, including from initially off. Disabling metrics leaves traces enabled. An admitted export remains pending until its actual outcome; disabling emission does not cancel it. New controls do not hold the application's observation lock while waiting for an SDK acknowledgement.

Module entry is also native: replace `--entry ...` with `--module package.module`. Place app arguments after `--`. `--plain` starts the same native source without instrumentation or SDK runtime files.

## What is observed

Sources use the common root-relative glob selectors. Function identity is the dotted relative filename, without `.py`, followed by the code object's qualified name. Exclusions take precedence. The tool does not replace functions, code objects, signatures, arguments, return values or exceptions. Native `sys.monitoring` distinguishes completion, unwinding, suspension and resume. Suspended/censored work is never reported as a finished span.

The app-side official tracer has a public OS-entropy ID generator and a primitive span processor. It does not run the OTLP exporter or metric aggregator. The [owned SDK process](python-sdk-process.md) handles aggregation, batching, retries and export without consuming app pseudo-random state. Tracing uses parent-based ratio sampling.

Selected calls produce `otelc.function.calls`, `otelc.function.duration` and `otelc.function.exceptions`. Duration uses a monotonic clock and stops before completion-side telemetry work. A call must enter and complete with collection enabled to contribute function metrics. An off/on interval inside a call can remain part of its elapsed time; these counters do not claim continuous recording during that gap. Metric records carry the existing span context to the official SDK exemplar pipeline. Export state and source completion counts are separate from downstream receipt.

Exception probes read only the raw exception type name; they do not invoke application `str`, `repr` or exception message getters. Failure recovery abandons the probe's context without inventing a completed span and retains the loss in final evidence. SDK span, event and link attribute-drop counts survive the process bridge.

The generated startup hook restores original import paths/PYTHONPATH, chains the original `sitecustomize` through CPython, and removes only its temporary module when no original exists. Startup failure exits before application code rather than allowing CPython to swallow a failed instrumentation hook. An incomplete control client has a total read deadline and cannot hold up app shutdown.

## Task and thread context

`[context] tasks=true` installs the pinned [official OpenTelemetry threading instrumentor](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/threading/threading.html) before application code starts. It transfers the current OpenTelemetry context through ordinary `Thread`, `Timer` and `ThreadPoolExecutor.submit` calls. It adds no spans itself: selected application functions provide the visible parent/child spans. The same mechanism carries baggage and restores context after a worker returns or raises.

Native [asyncio tasks](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.Task) already copy the current context. Their existing behaviour remains when `tasks=false`; that setting disables our additional thread patches and does not remove application-installed instrumentation. Context switches are launch-time settings. Metric collection/emission still switch live without disabling context or tracing.

The ordinary [TaskApp.py](../examples/python/TaskApp.py) reuses one executor, starts a thread/timer, awaits asyncio work and preserves a future exception. It contains no telemetry imports or annotations. Run it with the shared [task-only policy](../examples/tasks.toml):

```sh
runtime="$(mktemp -d /tmp/otelc-python-tasks.XXXXXX)/runtime"
.venv/bin/python -m otelc --config examples/tasks.toml python \
  --root "$PWD" --entry examples/python/TaskApp.py --runtime "$runtime"
```

It prints `{"results": [70, 98], "failure": "negative seed"}`. The policy uses common function-name globs; qualified nested functions that match them are also selected. The test suite checks both policy states with a real receiver, plus baggage and context isolation on reused workers. Thread subclasses which replace `run`, custom executors, process pools, forks and arbitrary scheduler implementations are not qualified. The separate [HTTP candidate](python-http.md) covers ordinary urllib/WSGI clients and servers.

## Object and resource lifetimes

Enable `[lifetimes]` and select dotted class names, using the same source selectors and policy fields as the other adapters:

```toml
[lifetimes]
enabled = true
include = ["*WorkItem"]
release_methods = ["close"]
max_live = 4096
```

The original `WorkItem` needs no telemetry code. Construction is admitted after its ordinary class call and Python initializer succeed. A failed initializer or `__new__` never becomes a completed lifetime. Inherited, wrapped and dataclass-generated Python initializers are observed without replacing the class or its methods. Explicit calls to `__init__` are not new construction.

An object has separate `observed_gc` and `resource` boundaries. Successful return from a configured Python release method completes its resource boundary once. Exceptions and async cancellation leave it open for a later retry. Async methods complete at their final return, not at their first suspension. A synchronous release that returns a coroutine, generator or awaitable is unsupported and records loss; returning deferred work does not prove release.

Weak references observe GC without retaining the object, its frames or its class. The observer never forces application collection. GC before release records an incomplete resource. Objects still alive at shutdown are censored, with no fabricated GC or release span. Lifetime spans have their own trace roots and link to the creating context when it exists. Their duration starts after successful construction and ends at the observed boundary; it is not allocation-to-free time.

GC callbacks record the observed monotonic time and collection state in a queue bounded by tracked instances and classes. They do not take the runtime lock or call the SDK: GC can occur while an SDK writer lock is held. Selected observations, status/flush, control updates and shutdown drain this queue under the runtime lock, releasing the queue lock before publication. Reporting delay does not extend the lifetime duration. GC observed while collection is off does not become a completion metric after re-enable. Shutdown drains already observed GC before censoring survivors.

A collection switch seals the previous GC interval under the queue lock. It publishes that interval before the SDK control record, while later callbacks capture the requested new state. The queue lock stays released during all SDK writes. A callback during an in-progress control handoff therefore belongs to the new interval; it cannot silently use the old flag and then lose its metric after the switch. Failed handoffs retain the runtime control loss and cannot become a healthy final report.

Metrics use `otelc.lifetime.admitted`, `completed`, `incomplete`, `lost`, `duration` and `live`, under the `io.quux.otelc.lifetimes` scope. Type and boundary labels distinguish observations. Live gauges use `type=all` and resynchronise when collection is enabled. Completion metrics require admission and completion with collection enabled; tracing stays independent. Status and the final SDK checkpoint include admissions, failures, live/censored objects and explicit loss reasons.

This profile requires a Python initializer and weak-reference support. Default/C-only initializers, instances without weak references, custom metaclass `__call__`, non-Python release descriptors and capacity exhaustion record explicit losses and prevent a clean final report. This does not cover every allocation, implicit builtin resources, finaliser resurrection, forks or arbitrary metaclass construction. It does not claim exact destruction time.

To run the unchanged example with the full shared policy:

```sh
runtime="$(mktemp -d /tmp/otelc-python-lifetimes.XXXXXX)/runtime"
.venv/bin/python -m otelc --config examples/otelc.toml python \
  --root "$PWD" --entry examples/python/BenchmarkApp.py --runtime "$runtime"
```

Enter `10` for ordinary work, `lifecycle` for constructor failure/release retry, and `quit` to drain the SDK. `gc` asks the example itself to collect for demonstration; it is not a production observer action.

## Evidence and current limits

Local integration tests use CPython 3.12 and the locked SDK. They compare original script/module metadata, arguments, source hashes, seeded random values, signatures, output, tracebacks and exit codes against native baselines. Real receivers decode the four-state example's metric counts and spans, bound to the actual application PID rather than the CLI/SDK PID. Separate tests cover recursion/context boundaries, async suspension/cancellation, occupied monitoring IDs, controlled startup/probe failures, queue/transport evidence and open control connections. These are tested profiles, not a claim about arbitrary applications.

The launcher currently uses otelc's interpreter/environment; install app dependencies there. Arbitrary interpreter/virtualenv selection, isolated/no-site modes, interactive/zip/bytecode entry, fork/process observation, arbitrary context manipulation, annotation reading, complete source provenance and common `health` presentation are not yet qualified. The [HTTP candidate](python-http.md) now accepts the common HTTP switch for its declared urllib/WSGI profile. The [paired benchmark workflow](benchmarks.md) runs the ordinary Python example in plain and four same-PID metric states and publishes driver measurements to the configured Collector. Raw samples and separate app/SDK CPU/RSS windows remain available locally. These functional checks do not establish stable performance estimates.

Run the focused tests from a prepared checkout:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_monitor test_python_native test_python_frontend_components \
  test_python_lifetimes test_python_lifetime_components \
  test_python_lifetime_locks test_python_lifetime_controls test_python_tasks -v
make check
```

CPython suppresses trace events while running monitoring callbacks. Coverage therefore combines real VM integration with direct internal component tests; the latter are test fixtures, not required developer code changes. Required main-target checks, independent review and immutable-provider oteleq acceptance still apply before Python can be promoted to implemented.

Function selection caches excluded code by weak identity, up to 4096 entries, to avoid repeating filename resolution for SDK and other unselected functions. The cache does not retain code or its constants, frames or application objects. It checks reference identity before a hit, releases entries when code is collected, and clears on monitor shutdown. At capacity, selection still runs normally for uncached code; capacity does not suppress telemetry. This uses the launch's fixed selection policy and does not disable lifetime monitoring callbacks. The focused regression counts filesystem resolutions, rather than asserting a host-dependent latency target. Global monitoring, tracing and lifetime bookkeeping still add cost when metric collection/emission are off; use separately labelled trace/lifetime policies to isolate those costs in the benchmark workflow.
