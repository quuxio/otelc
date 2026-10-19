# Python instrumentation without source edits

The Python adapter runs original applications with CPython 3.12+ execution monitoring and the Python OpenTelemetry SDK. It does not rewrite functions, change signatures, import application decorators or add frames around application functions. The [monitoring API](https://docs.python.org/3.12/library/sys.monitoring.html) distinguishes normal returns from exception unwinding. Durations are wall time from first execution until final return/unwind, including suspension for generators and coroutines.

## Install and run

```sh
make setup
make build
./target/debug/quux-otelc --config examples/python.toml --language python doctor
./target/debug/quux-otelc --config examples/python.toml --language python inspect examples/apps/python_app.py --json
make stack-up
./target/debug/quux-otelc --config examples/python.toml python examples/apps/python_app.py
./target/debug/quux-otelc --config examples/python.toml python examples/apps/python_annotated.py
```

The CLI uses the checkout's `.venv/bin/python` when present, otherwise `python3`. Set `OTELC_PYTHON` to another CPython environment containing the hash-locked dependencies. Use `OTELC_ADAPTER_ROOT` if distributing the adapter directory separately from the checkout. Module launch uses `python -m MODULE [ARGS...]`. Original `sys.argv`, imports, application exit status and exceptions follow normal script/module execution.

The unchanged example produces `151`. It exercises selected functions, recursive calls, ordinary threads, exceptions caught in a caller, generators, async suspension and cancellation. The annotated example produces `30`; only `selected` and externally configured `configured` are measured.

See [collection lifetime spans](python-collection.md) for independent external type selection, collection metrics, creation links and qualified class shapes.

## Common selection and optional metadata

Use `examples/python.toml` or the same schema-2 document used for other languages. `sources` matches original files relative to the launch working directory. Function display names are the relative Python path without `.py`, with `/` changed to `.`, followed by `code.co_qualname`. For example, `examples.apps.python_app.process_order`. Anonymous code identities append their original line and column, keeping same-line lambdas distinct. Nested names remain visible in the optional report.

No annotations are required. If `annotations.read_existing = true`, existing comments immediately preceding a function/decorator block can select or exclude it:

```python
# otelc.instrument
def selected(value):
    return value + 1

# otelc.exclude
def excluded():
    return 20
```

External exclusions and `otelc.exclude` always win. Unknown otelc comments are rejected. SDK, adapter, virtual-environment and external interpreter files are protected from self-instrumentation. The adapter consumes the shared resolver's byte-oriented selection expressions rather than translating globs independently.

Selection is cached by code-object identity. Identical function bodies in separate files retain separate names and cannot bypass another file's exclusions.

## Metrics and live control

The adapter exports cumulative `otelc.function.calls`, `otelc.function.unwinds`, duration histograms in seconds, dropped-observation counters and dropped-batch counters through OTLP/HTTP protobuf. Shared service/resource settings, explicit histogram buckets, final signal endpoint and environment/header precedence apply. The SDK exporter operates on one batch at a time, below `export.max_queued_batches`; export happens away from application threads. Failed snapshots are retried on the next collection; only successful snapshots enter duplicate suppression. An idle successful exporter stops sending unchanged snapshots, including after metrics-off frames have drained.

To enable owner-only live control, add `runtime.control_socket` and create its private parent directory:

```sh
mkdir -p build/python-control
chmod 700 build/python-control
# Add control_socket = "build/python-control/metrics.sock" under [runtime].
./target/debug/quux-otelc --config YOUR_CONFIG python YOUR_APP.py
# In another terminal:
./target/debug/quux-otelc status --socket build/python-control/metrics.sock
./target/debug/quux-otelc disable --socket build/python-control/metrics.sock
./target/debug/quux-otelc enable --socket build/python-control/metrics.sock
```

Calls admitted before disable finish normally, including exceptional exits. Calls started while disabled stay unmeasured after enable. There is no restart or rebuild between phases. When metrics are off, start callbacks are disabled. Return/unwind callbacks remain only until admitted frames drain, then monitoring events are switched off. Enabling during an unobserved coroutine does not silence the return event of future measured calls. The launcher and SDK remain present; disabled overhead is still measured by the benchmark. A socket cannot replace an occupied path and normal shutdown removes only the socket it owns.

Set `OTELC_REPORT_PATH` to retain bounded function names, counts, unwind counts, losses, application PID and exporter shutdown evidence. `runtime.max_functions` bounds selected identities and `runtime.max_active_calls` bounds outstanding observations, including suspended frames. Unfinished/abandoned frames are counted as incomplete at shutdown; their objects are not retained merely to collect metrics.

## Validation and overhead

```sh
make python-check
make benchmark-language LANGUAGE=python
```

The Python product gate requires 80% coverage separately from native coverage. Tests decode actual SDK protobuf exports, compare plain/instrumented output, verify unchanged original bytes, and exercise exclusions, optional comments, exceptions, recursion, threads, generators, cancellation, bounds and owner-only controls.

The benchmark uses `examples/apps/python_latency.py` unchanged. It keeps one plain process and one instrumented application alive, alternates metrics off/on, checks identical checksums and requires exact expected observations, zero losses and completed export. Raw samples and toolchain/host evidence are saved under `build/benchmarks/python/`. Absolute added nanoseconds per call and disabled-monitoring overhead are reported separately; timings are workload-specific.

See the [Collector/Grafana guide](observability-stack.md). Select service `otelc-python-example` or query `otelc_function_calls_total{service_name="otelc-python-example"}` in Prometheus.

## Boundaries

This is a function-metrics adapter for CPython 3.12+ on the tested macOS/Linux CI lanes. C-extension internals, PyPy, subprocess/fork propagation, distributed spans, annotation injection and automatic destruction/resource lifetimes are unavailable; ordinary-class collection spans are qualified separately in the [collection guide](python-collection.md) and requested unsupported capabilities fail configuration resolution. Generator/coroutine timing begins at first execution, not object allocation. Fatal process termination cannot guarantee export. Source/class behaviour is preserved; full debugger/profiler combinations still require qualification, and otelc claims a free monitoring ID rather than replacing another tool.

Function spans can be enabled through the same external policy; see the [Python span guide](python-spans.md) for unchanged source, coroutine/generator parenting, loss and viewing commands.
