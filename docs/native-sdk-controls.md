# Native SDK metric controls

<!-- markdownlint-disable MD013 -->

Status: development component. This adds independent collection/emission controls around the official OpenTelemetry C++ SDK 1.30.0. It is a building block for C/C++ and the native compiler pipeline, not a delivered application adapter. Native common-policy launch commands, probe ABI linking, OTLP export, context propagation, object/resource lifetimes and equivalent application benchmarks remain work.

Developers will not add these classes or SDK calls to application source. The C++ fixtures are internal component tests; the native frontend must generate its own bootstrap and consume the common TOML policy. The [LLVM compiler component](llvm-backend.md) inserts selected calls separately.

## Behaviour

The controller uses the official `MeterProvider::UpdateMeterConfigurator` API for live collection changes. It updates existing and future meters, including instruments cached while initially off. SDK observable callbacks stop while their meter is disabled. An official `MetricFilter` suppresses held synchronous snapshots during collection-off; SDK disabling alone still permits those old cumulative points to be collected.

Emission uses a small `PushMetricExporter` delegate. It starts no new delegate exports or flushes while either collection or emission is off. Emission-off does not change collection, so later cumulative points retain measurements from the silent interval. The wrapper requests cumulative temporality from the SDK. Failed exports remain failures and increment the observed failure count; a later cumulative collection retains their measurements. Actual network retry policy and downstream receipt are separate, unimplemented acceptance steps.

Status separates wrapper calls waiting for exporter serialisation (`queued`) from admitted delegate operations (`in_flight`). Off returns without waiting on network I/O; an admitted call can finish. A strict SDK-call off barrier therefore requires both counts to settle to zero. Queued calls recheck the current switches before admission. These counts do not expose a delegate's internal HTTP queue or prove Collector receipt.

Shutdown closes admission before waiting for the current delegate operation, honours its supplied wait budget, and can be retried after a timeout or failed delegate shutdown. Callers must keep the object alive until its outstanding work has settled. A closed exporter refuses further exports, flushes and control changes. Collection updates and export serialisation use separate locks, and status locks are released before SDK calls. These controls do not alter the SDK tracer provider.

## Contributor checks

Install LLVM 22, CMake 3.22+ and Ninja, then run:

```sh
brew install llvm@22 cmake ninja
make native-check
```

Linux CI installs the same build dependencies through `ci/install_llvm22.sh`. CMake fetches the official SDK from commit `c7b64ec660c1b6c65dff0932b3d1c3483bc6db64`, checks archive SHA-256 `2e1df07f6217833e13819d12716f52fa93103dfe6b8826c76fb2868da339712f`, and verifies HTTPS. A fresh private build prevents reuse of stale native test products. The check uses two compiler jobs, then removes SDK objects and temporary sources after retaining the test executable, merged profile and compact source/tool/command/coverage record under `build/native-control*`.

The tests use actual official SDK meters, cumulative aggregation, observable callbacks and the trace processor. They cover all four startup states, cached/new instruments, silent collection, emission restoration, failed-export recovery, blocked and queued calls, bounded shutdown, tracing independence and concurrent status callbacks. Export sinks are diagnostic fixtures; this does not test a real OTLP HTTP exporter or a Collector/Grafana pipeline.

`make native-check` independently enforces at least 80% LLVM line coverage for each native control implementation and at least 80% Python coverage for its check driver. Negative checks reject missing, duplicated, foreign or under-covered source reports. The full `make check` includes this check and combines the driver's actual execution coverage with the normal Python report. Independent review and required hosted gates remain necessary before delivery. C++ Sonar analysis is not yet configured for this component.
