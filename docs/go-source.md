# Go source adapter

Status: implementation in progress. The [SDK controls](go-sdk-controls.md) are delivered as a foundation. The source adapter still needs public build/launch commands, runtime/OTLP/health integration, configured function/error/lifetime probes, paired benchmarks and independent oteleq qualification. `plan` continues to report Go as remaining.

## Upstream compiler and context reuse

`make go-upstream` builds the official `go.opentelemetry.io/otelc` compiler wrapper at version 1.0.1 using Go 1.27.3. It verifies the module and go.mod checksums, embedded instrumentation bundle, executable build metadata and reported tool version. Compiler, executable and bundle identities are retained in `build/go-upstream/toolchain.json`. These are tooling inputs, not evidence that a particular application was instrumented.

The build environment pins `GOROOT`, `GOTOOLCHAIN=local` and the selected compiler's directory at the start of `PATH`. The wrapper runs a nested `go` command, so passing an absolute compiler to the stock build alone is insufficient. Every compiler fixture independently checks that nested `go env` reports the same Go version and root before building.

The context preparer copies a fixed subset of the official runtime and SDK trace hooks into an owned private build. Original application source and modules are preserved. It reuses the upstream goroutine-local context storage and span hooks; it does not parse goroutine IDs from stack text. Only the context bridge is included from the upstream runtime package. Its separate SDK/logging/signal initialisation is excluded so that the common-policy runtime can own those pipelines.

The shared `context.tasks` switch controls automatic goroutine inheritance. Disabled inheritance still resets the added runtime fields when a goroutine is created: Go can reuse goroutine structures, so omitting that reset can retain unrelated old context. Enabled inheritance applies a small, explicit generated-source correction to upstream `Clone`: it captures immutable parent span context rather than sharing the parent's ended flag. A child released after its parent finishes therefore retains the captured causal parent. Copied upstream files, generated modifications and their separate hashes are recorded; verification reproduces their bytes from the pinned original bundle.

## Focused checks

```sh
make go-upstream
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_go_context_bundle test_go_upstream_builder test_go_context_integration
```

The actual compiler tests build ordinary source with the pinned upstream tool and original sources with stock Go. Both return the same value. They check immediate/delayed children, disabled task inheritance and reuse of goroutines containing unended diagnostic context. The generated hooks use the candidate owned trace processor with the official SDK and an in-memory exporter. Its private SDK copy and original inputs are checked after compilation and execution. Generated hooks import telemetry tooling; the original fixture does not.

These checks establish the declared context/tooling behaviour only. They do not establish public Go launching, metrics integration, HTTP/framework propagation, resource/object lifetime completeness, whole-program source provenance or language admission.

The generated context bridge also has an explicit non-ending detach operation for owned lifetime spans. It removes only that span's implicit frame and shared propagation-state entry, restores the previous local root and leaves the SDK span open with its captured parent. Later functions/tasks therefore keep their function parent while a lifetime span remains open. Both recording and non-recording spans are checked with the actual compiler; disabling task inheritance still prevents automatic parent capture.

The helper's entire algorithm is also compiled against real SDK spans with fixture TLS/link endpoints. The component test requires 80% statement coverage and checks invalid/missing spans, middle/root/head removal, repeated removal, retained open spans and propagation-state cleanup. The separate compiler tests check the actual TLS/hooks/tasks integration. This is lifetime-context infrastructure; automatic object tracking and GC/release boundaries remain runtime work.

## Owned SDK export scheduling

The candidate `pipeline` package uses an official SDK manual metric reader with the existing live controls. It schedules exports itself so an exporter failure is recorded locally and does not invoke the application's global OpenTelemetry error handler. Export errors remain errors, cumulative measurements survive failed exports and emission pauses, and application-owned global providers remain unchanged.

The separate owned trace processor admits completed sampled SDK spans to a bounded queue without blocking application completion on network IO. Record-only spans follow the official SDK's sampling decision and do not export or count as queue loss. The processor retains failed batches for the next drain, reports queue capacity losses and pending spans, and permits delayed children after their parents have already exported. Each flush covers the spans present at its start; concurrent producers cannot make it run indefinitely. Shutdown cancels periodic work, stops admission and records the final drain/exporter outcome. These counters describe exporter API outcomes; independent decoded downstream receipt is still required.

The candidate OTLP transport reuses the official metric and trace HTTP/protobuf exporters at 1.47.0. Resolved signal endpoints, headers and timeouts are explicit. Requests use bounded acknowledgement reads, refuse redirects and require a complete successful protobuf response. Partial rejection, warning-only partial responses, malformed/oversized replies and HTTP errors return export failures. A validated empty acknowledgement is then passed to the SDK so its partial-success handler cannot route the collector response to the application's global handler. Pipeline scheduling owns retries; the trace queue retains failed batches and metric snapshots use cumulative aggregation.

Retry does not establish exactly-once delivery: a collector can accept data before a response is lost. Failure counters remain visible after recovery. Qualification must inspect them and decoded downstream data. Malformed ambient SDK environment configuration and application-specific SDK/provider interactions still require launcher validation; explicit exporter options alone do not prove those cases.

`make go-check` includes race tests and an 80% statement floor for every control and pipeline source file. Tests use both injected exporters and real loopback HTTP receivers that decode the official protobuf payloads and exercise acknowledgement failures followed by recovery. Public source instrumentation, actual Collector receipt and common control/health integration remain to be implemented before Go qualification.

## Generated function and lifetime probes

The candidate `probe` package is now connected to actual upstream compiler rules in an internal ordinary-source fixture. Generated hooks enter selected functions and defer their completion, observe successful pointer-returning factories and observe existing release methods. Original application source, its module file, output and error values are preserved. A private generated tooling file supplies the SDK and context bridge; developers add no telemetry code. This fixture is not yet a public policy-driven builder or launcher.

Function completion uses bounded runtime frame metadata up to the selected invocation. It does not recover/rethrow a panic or call application error formatters. Escaping panic, `runtime.Goexit` and a non-nil declared error return have separate outcomes. A typed-nil error interface remains non-nil. A function called by an outer panic defer can still complete normally. An absent or truncated invocation boundary reports unknown; it neither claims a normal return nor invents an application error. The selected SDK uses `WithoutPanicRecording` so its span end does not recover or format the application's panic. Exact unhandled-panic stack presentation and arbitrary compiler/platform profiles remain unqualified.

Lifetime identity uses typed `weak.Pointer` keys and `runtime.AddCleanup`. The metadata and cleanup argument do not retain the application object. A successful selected release ends only the resource observation; a failed, panicking or duplicate release cannot invent completion. GC ends only the observed-GC observation. An object collected before a successful release leaves the resource boundary incomplete. Instrumentation does not request GC. Cleanup notification has no precise collection-time or execution guarantee; objects below 16 bytes are currently refused to avoid the small-allocation profile. Broader allocation sites, aliases through different concrete pointer types and arbitrary factory/release signatures require build-time resolution and qualification.

The probes reuse the common `otelc.lifetime.admitted`, `completed`, `duration`, `live`, `incomplete` and `lost` metrics with `type` and `boundary` labels. Boundaries are `observed_gc` and `resource`. Completed lifetime spans are separate traces with a link to construction context. Their open SDK frames are detached immediately, so an unrelated function or task does not inherit a lifetime span. Shutdown stops owned cleanup observations and retains unresolved objects/resources in local health; it does not end them at a fabricated GC or release time. Duplicate tracking is counted separately from loss. Capacity, unsupported bindings/sizes and failed context detachment remain visible as observer failures.

The recorder limits active owned functions to at most 512 and uses the configured bounded lifetime capacity. This does not qualify interaction with arbitrary application-owned SDK context or upstream context-table pressure. The public launcher must still define/refuse unsupported profiles and expose the complete health evidence.

```sh
make go-check
PYTHONPATH=tests .venv/bin/python -m unittest -v test_go_probe_integration
```

The component checks use real SDK meters/spans, cached collection gates, concurrent starts, startup-off, weak GC, successful/failed/duplicate releases, pending shutdown lifetimes, unknown stacks and instrument-construction errors. The compiler fixture compares actual stock and woven applications with recording enabled/disabled and sampling enabled/disabled, retaining separate counts and SDK/context evidence. The fixture explicitly requests GC; production probes do not. These checks do not establish live public control commands, downstream Collector receipt, benchmarks, broad source coverage or oteleq admission.
