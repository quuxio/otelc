# Native function SDK engine

<!-- markdownlint-disable MD013 -->

Status: development component. The native engine connects registered LLVM function descriptors to official OpenTelemetry C++ SDK spans and call/error/duration instruments. The internal C/C++ fixtures are compiled by the registered LLVM profile and linked to a test-owned SDK bootstrap. The separate [native launch candidate](native-launch.md) adds generated application bootstrap, official OTLP/HTTP export, common-policy live controls and retained health. Broader native language qualification, task/service propagation, automatic object/resource lifetimes and equivalent application benchmarks remain work. This component does not by itself deliver a C/C++ application adapter.

Application developers will not add this engine, SDK imports or probe callbacks. The fixture source files contain ordinary C/C++ only. The tooling must eventually generate and own the bootstrap, providers and versioned ABI callbacks.

## Call behaviour

`FunctionRuntime` copies the compiler's address/name descriptors before accepting calls. A complete batch must fit the configured function limit, with non-empty names of at most 4,096 bytes. Conflicting names for one address, invalid descriptors and capacity overflow refuse the batch without partially registering it. Existing function names stay stable while calls are active.

Each admitted call owns one bounded frame and a unique non-zero token. The SDK span becomes the current OpenTelemetry context until the same thread leaves that call. Nested probes and application SDK spans therefore use normal SDK parenting, and exit restores the previous context without ending the application's parent span. Exits must be in stack order on the owning thread. An invalid token, foreign-thread exit or invalid unwind flag cannot remove a valid frame. A zero token has no exit action.

The engine records `otelc.function.calls`, `otelc.function.errors` and `otelc.function.duration`, labelled by `function`. A compiler-reported exceptional exit marks the span as an error and adds `error.type = "exception"` to the error metric/span. It does not inspect application exceptions or invoke their formatters. The duration uses the steady clock and includes the engine's entry work.

The tooling must call `ConfigureFunctionMetrics` once on its owned provider, before creating these instruments in `FunctionScopeName`. This installs three exact SDK views, with enough cardinality for the selected function limit and the common policy's `histogram_boundaries_seconds`. Defaults match the shared policy. Boundaries must contain 1–128 finite, positive, strictly increasing values. Views are restricted to these exact instrument names, units and scope. The engine does not reconfigure application-owned SDK meters. Reader-level cardinality settings alone are insufficient in the pinned SDK, and its default limit of 2,000 would merge larger selections into overflow series. A 4,096-function regression checks every call/error/duration series and rejects overflow. Another test checks non-default second-based buckets.

Metric collection must be enabled at both entry and exit for the completed call to record metrics. A call that starts while collection is off does not become a measurement when collection is enabled midway through it. Existing [SDK meter controls](native-sdk-controls.md) also suppress collection/export while off. Emission-off retains cumulative measurements for restoration. Neither metric switch changes tracing. A null tracer selects a metrics-only engine.

Function registration defaults to a maximum of 4,096 functions and 512 active calls. Smaller positive limits can be selected by the tooling. Capacity, unknown-function and re-entry refusals return zero and increment loss accounting. SDK processor/exporter callbacks may inspect status without a bookkeeping lock being held across SDK calls. A callback that invokes another selected function is refused with visible loss, rather than recursively entering instrumentation.

`Close()` stops admission and returns false while any admitted call is still active, including an exit inside an SDK callback. The owner must retain the engine and providers until those calls leave on their owning threads and `Close()` succeeds. Active calls are not converted into successful completions. This API is a component lifetime contract, not a process-shutdown or network-drain implementation.

The default `FunctionContext::ambient` mode attaches SDK scopes, joining and restoring application SDK context. It requires the SDK thread-local stack to remain alive. `FunctionContext::owned` instead passes an explicit SDK root or the preceding selected function's context, without reading or changing ambient scopes. The native launch candidate uses owned mode so late global/TLS destructors can call selected functions after the SDK's thread-local stack has been destroyed. Owned mode does not inherit an independently installed application SDK scope.

## Checks

```sh
make native-check
```

The check builds the pinned official SDK and registered LLVM plugin in a fresh directory. The unedited fixtures are [C functions](../adapters/native/tests/fixtures/functions.c) and [C++ functions](../adapters/native/tests/fixtures/functions.cpp). The C fixture is compiled with exceptions enabled so a C++ exception can unwind through its C callback bridge. Optimised fixture calls exercise recursion, threads, C/C++ nesting and the original exception message. These SDK sinks are local diagnostic exporters, not OTLP HTTP or Collector receipt.

The actual SDK tests also check existing application parents and child SDK scopes, all four initial metric states, cached instruments, silent collection/restoration, collection changes during calls, wrong-order/foreign/duplicate exits, bounded admission, atomic registration, copied UTF-8 names, metrics-only operation, incomplete close and exporter re-entry. Each native implementation has its own 80% LLVM line floor. The Python driver and exact-source Sonar conversion have separate coverage checks. Actual compilation context includes both native implementations, both SDK test units and the ordinary C/C++ fixture units.

Ordinary C++ exception cleanup is supported by these component checks. `longjmp`, forced thread termination, process abort, moving a live frame to another thread, misnested application SDK scopes, dynamic image unload/reload and arbitrary native build recipes are outside this component's qualified boundary. Task/service and lifetime instrumentation need their own implementation and evidence; balanced function spans do not establish them.
