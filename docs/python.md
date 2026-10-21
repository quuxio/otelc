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

See [collection lifetime spans](python-collection.md) and [resource lifetime spans](python-resources.md) for independent external type selection, metrics, creation links and qualified class/release shapes.

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

Metrics and trace HTTP requests use one monotonic elapsed budget through DNS, TCP/TLS connection, response headers/body and retries. The configured transport budget overrides the SDK's wall-clock-derived timeout, so clock corrections cannot extend it or cancel a valid request before sending. Shutdown also clamps new requests to its remaining budget. The transport accepts only complete HTTP 200 OTLP acknowledgements, with a 64 KiB decompressed response limit; redirects and partial rejection fail the batch. At most six wire attempts share the original deadline, using exponential backoff with jitter and `Retry-After` when it fits. The SDK continues to own protobuf encoding and compression; these steps precede the transport deadline.

The private transport uses one daemon watchdog and two daemon DNS workers, two queued DNS jobs and 64 deadline slots. It cancels its own TCP descriptors without patching application sockets or global urllib3 pools. An OS DNS lookup cannot be forcibly interrupted: the caller times out, late results cannot send a request, and stuck lookups consume the fixed worker capacity until they finish. Subsequent exports fail visibly rather than creating more workers. Direct HTTP/HTTPS, verified TLS, ordinary HTTP proxies and pooled connections have regression coverage; HTTPS proxy chains still need end-to-end qualification. SOCKS proxies are explicitly unsupported. These bounds allow scheduling tolerance and are not a hard real-time guarantee.

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

Owned Python providers use independent random generators for span/trace IDs, export jitter and reservoir exemplar sampling. They leave the application's module-level `random` state and unrelated SDK providers unchanged. Exemplars and the SDK's random-ID flag remain enabled. This integration depends on the hash-locked SDK's private HTTP-client jitter callback and fixed-size reservoir hook; dependency upgrades must rerun the seeded-output, successful/failed-export and exemplar controls. It does not claim isolation from every form of application global state or timing.

## Boundaries

This is a function-metrics adapter for CPython 3.12+ on the tested macOS/Linux CI lanes. C-extension internals, PyPy, subprocess/fork propagation, distributed spans, annotation injection and automatic destruction lifetimes are unavailable; ordinary-class collection and explicitly bound resource spans are qualified separately in the [collection guide](python-collection.md) and [resource guide](python-resources.md) and requested unsupported capabilities fail configuration resolution. Generator/coroutine timing begins at first execution, not object allocation. Fatal process termination cannot guarantee export. Source/class behaviour is preserved; full debugger/profiler combinations still require qualification, and otelc claims a free monitoring ID rather than replacing another tool.

Function spans can be enabled through the same external policy; see the [Python span guide](python-spans.md) for unchanged source, coroutine/generator parenting, loss and viewing commands.

Python monitoring stays active through ordinary non-daemon thread completion and application `atexit` callbacks. The adapter registers its final cleanup before running application code, so application callbacks run first. It then disables monitoring and drains telemetry within the configured shutdown budget. It does not join or terminate application threads itself; unfinished daemon work remains an incomplete observation. Fatal termination still cannot guarantee export.

Valid script, bytecode, module/package and directory/zip launches retain the application's persistent `__main__` namespace and ordinary exception output. The interpreter handles its original exception object and any application-defined `sys.excepthook`; otelc removes only its own leading launch frames, without replacing the hook or filtering application frames by filename. Module/archive routes use CPython's normal entry machinery, preserving its native traceback frames and metadata. Raw baseline/CLI comparisons cover chained errors, notes, exception groups, custom hooks, interrupts, exits and passive exception attributes on CPython 3.12 and 3.14. Instrumentation setup failures retain their diagnostic stack.

Python metrics and trace headers use independent signal-specific overrides. An explicitly empty metrics header variable suppresses generic OTLP headers at the actual HTTP boundary. Metrics retain the SDK's `none`, `gzip` and `deflate` compression options; unsupported compression values fail before launch without echoing their contents.

Script launches preserve CPython's absolute `__file__` spelling, including a supplied `./` component, and the original `sys.argv[0]`. They supply the ordinary script loader metadata, so resource lookup relative to the script continues to work after `chdir`. Source and precompiled-bytecode script controls cover relative, absolute and space-containing paths without rewriting inputs. Module (`-m`) metadata and the existing directory/zip entry-point path are retained.

The application retains its own `__main__` module through exit callbacks. Script, bytecode, module, package, directory and zip launches use normal code loaders and metadata, with application globals/classes and module identity retained. Its initial annotation namespace follows the interpreter, including lazy module annotations in Python 3.14. Cleanup code can import `__main__` and pickle its classes; launcher globals are not injected into the application namespace. Loaders without executable code fail explicitly before target execution.
