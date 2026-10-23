# Python SDK metric gates: implementation in progress

<!-- markdownlint-disable MD013 -->

This is a building block for the Python rebuild. It does not provide a source-free Python launcher, advertise Python as implemented, or complete Python qualification. Node remains the preceding language candidate and must complete its own acceptance and merge first.

## Expected behaviour

One schema-3 policy must drive the ordinary Python application without telemetry imports, decorators, annotations, bootstrap edits or changed source files. Metric collection and emission must switch independently in the same application instance, including enabling from startup-off; tracing stays independent. Object observed-GC and successful resource release are separate boundaries, with failed, lost and censored observations reported explicitly.

`otelc.python_metrics` provides public metric API wrappers for the official pinned Python SDK. Counters, up/down counters, histograms and synchronous gauges keep their SDK registration, scope, attributes, context, aggregation and cached instrument identity. Observable callbacks run only while collection is enabled. A collection transition invalidates a callback's old generation, including an off/on cycle before it returns. Points already admitted before the transition retain cumulative SDK semantics. Emission alone does not invalidate a callback generation.

Synchronous wrappers also expose the actual SDK instrument's public `name`, `unit`, `description` and `instrumentation_scope`. The scope object and cached wrapper identity are preserved. Histogram advisory boundaries remain with the registered SDK instrument and are checked through real aggregated output; no private SDK advisory field is exposed as a new public API.

Generator callbacks are not primed while collection is off. Their setup runs at the first enabled collection, and later sends retain the SDK callback-options protocol. Collection-off does not stop all SDK maintenance or cancel an already executing application callback. A synchronous measurement admitted before a transition can finish in the SDK after that transition.

The exporter wrapper admits calls only when both switches are enabled. It suppresses cached snapshots while collection is off. Cumulative values recorded with emission off remain available when both switches are enabled again. An already admitted export is not cancelled by emission-off; status reports its pending count, and `effective_emission_off` becomes true only when it settles.

Raw export and flush outcomes remain visible, including exceptions. SDK `PeriodicExportingMetricReader.force_flush()` can return true after its exporter returns failure, or after the exporter flush returns false. The wrapper therefore retains separate failure and pending counters. The owned process pipeline checks the counters and actual collection outcomes before acknowledging flush or shutdown; the stock reader return alone is insufficient evidence.

Shutdown closes new export admission and delegates once. A repeated caller waits for the original result and can time out without starting another shutdown. A failed shutdown stays failed. The pinned SDK reader passes its remaining shutdown budget under `timeout`; the adapter preserves it when forwarding the exporter's public `timeout_millis` argument. The wrapper does not make a blocking delegate cancellable or prove a clean SDK drain by itself.

The API currently exports its experimental synchronous gauge type as `_Gauge`; the wrapper preserves that declared type. No private SDK meter configurator is used.

## Process boundary and reuse

The [SDK process bridge](python-sdk-process.md) places the SDK and HTTP exporters in a separate owned process and underpins the native frontend candidate. An actual successful export with the unmodified pinned SDK was reproduced changing Python's global `random` state: its HTTP retry client computes jitter even for the first successful attempt. SDK ID generation and exemplar sampling also use randomness. Keeping this work outside the application avoids patching SDK globals or application random functions and keeps SDK diagnostics off application output channels. The Node rebuild provides a proven process-boundary pattern to reuse.

The [native frontend candidate](python-native.md) uses CPython monitoring and the official application-side tracer/context/sampling APIs. It sends bounded completion/context observations to the SDK process, which owns aggregation, batching, exporters and retries. Weak lifetime observers, official thread propagation and the [urllib/WSGI candidate](python-http.md) use this boundary. Broader library profiles and independent review remain pending. The gate tests alone do not prove unchanged application RNG/output or source-free launch; separate native integration tests cover that declared candidate profile.

The archived monitoring/lifetime algorithms and focused failure cases can be reused selectively. Do not copy the old policy schema or turn the unresolved legacy Python export discrepancy into a new clean-drain claim. Keep legacy and rebuilt-provider qualification separate.

## Focused checks

From a prepared environment:

```sh
PYTHONPATH=tests .venv/bin/python -m coverage run --source=otelc.python_metrics -m unittest test_python_metrics -v
.venv/bin/python -m coverage report --fail-under=80
```

Tests use the actual official SDK and readers to check cached instruments, every supported metric kind, histogram buckets and exemplars, scope identity under concurrent access, disabled callbacks, generator callbacks, cross-thread and iterator generation changes, all four metric states, retained cumulative snapshots, failed export recovery, exceptions, in-flight emission-off status and concurrent/failed shutdown. These are component tests with explicit API calls, not developer example applications.

Remaining acceptance includes full source evidence, broader library/lifetime profiles, independent review, required CI/Sonar and immutable-provider oteleq qualification. The candidate now includes weak lifetimes, official thread/HTTP integration and paired benchmark/Grafana workflows. Native candidate tests now cover original sources, selected functions, same-PID controls and decoded OTLP for their declared profile. Python remains in the public plan's `remaining` list.
