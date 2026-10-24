# Go source adapter

Status: implementation in progress. The [SDK controls](go-sdk-controls.md) are delivered as a foundation. Candidate public `go-build` and `go` commands connect the official weaver, original-source selection, owned runtime, allocation/release probes, live controls and retained process/health evidence. The ordinary Go examples and [paired benchmark](benchmarks.md) use the same common policies as the other languages. The HTTP candidate below reuses the official wrapper and native compiler entries. Broader source/storage/library profiles, final gates and independent oteleq qualification remain work. `plan` reports Go as a candidate and keeps it in `remaining`.

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

This owned pipeline also suppresses metric snapshots while collection is off, including a switch during an observable callback and the final shutdown drain. It retains cumulative values for later re-enable. This is stronger than the standalone low-level exporter gate, which can forward an already-collected snapshot; the common runtime contract requires the reader and exporter together. Traces remain independent. A regression reproduces the old cached-snapshot transmission before the pipeline guard and passes with it.

The separate owned trace processor admits completed sampled SDK spans to a bounded queue without blocking application completion on network IO. Record-only spans follow the official SDK's sampling decision and do not export or count as queue loss. The processor retains failed batches for the next drain, reports queue capacity losses and pending spans, and permits delayed children after their parents have already exported. Each flush covers the spans present at its start; concurrent producers cannot make it run indefinitely. Shutdown cancels periodic work, stops admission and records the final drain/exporter outcome. These counters describe exporter API outcomes; independent decoded downstream receipt is still required.

The candidate OTLP transport reuses the official metric and trace HTTP/protobuf exporters at 1.47.0. Resolved signal endpoints, headers and timeouts are explicit. Requests use bounded acknowledgement reads, refuse redirects and require a complete successful protobuf response. Partial rejection, warning-only partial responses, malformed/oversized replies and HTTP errors return export failures. A validated empty acknowledgement is then passed to the SDK so its partial-success handler cannot route the collector response to the application's global handler. Pipeline scheduling owns retries; the trace queue retains failed batches and metric snapshots use cumulative aggregation.

Retry does not establish exactly-once delivery: a collector can accept data before a response is lost. Failure counters remain visible after recovery. Qualification must inspect them and decoded downstream data. Malformed ambient SDK environment configuration and application-specific SDK/provider interactions still require launcher validation; explicit exporter options alone do not prove those cases.

`make go-check` includes race tests and an 80% statement floor for every control, pipeline, probe and runtime source file. Tests use both injected exporters and real loopback HTTP receivers that decode the official protobuf payloads and exercise acknowledgement failures followed by recovery. The owned runtime integration below adds the common controls/health route; general public source instrumentation and actual Collector/Grafana receipt remain required before Go qualification.

## Generated function and lifetime probes

The candidate `probe` package is now connected to actual upstream compiler rules in an internal ordinary-source fixture. Generated hooks enter selected functions and defer their completion, observe successful pointer-returning factories and observe existing release methods. Original application source, its module file, output and error values are preserved. A private generated tooling file supplies the SDK and context bridge; developers add no telemetry code. This fixture is not yet a public policy-driven builder or launcher.

Function completion uses bounded runtime frame metadata up to the selected invocation. It does not recover/rethrow a panic or call application error formatters. Escaping panic, `runtime.Goexit` and a non-nil declared error return have separate outcomes. A typed-nil error interface remains non-nil. A function called by an outer panic defer can still complete normally. An absent or truncated invocation boundary reports unknown; it neither claims a normal return nor invents an application error. The selected SDK uses `WithoutPanicRecording` so its span end does not recover or format the application's panic. Exact unhandled-panic stack presentation and arbitrary compiler/platform profiles remain unqualified.

Lifetime identity uses typed `weak.Pointer` keys and `runtime.AddCleanup`. The metadata and cleanup argument do not retain the application object. A successful selected release ends only the resource observation; a failed, panicking or duplicate release cannot invent completion. GC ends only the observed-GC observation. An object collected before a successful release leaves the resource boundary incomplete. Instrumentation does not request GC. Cleanup notification has no precise collection-time or execution guarantee; objects below 16 bytes are currently refused to avoid the small-allocation profile. Broader allocation sites, aliases through different concrete pointer types and arbitrary factory/release signatures require build-time resolution and qualification.

The probes reuse the common `otelc.lifetime.admitted`, `completed`, `duration`, `live`, `incomplete` and `lost` metrics with `type` and `boundary` labels. Boundaries are `observed_gc` and `resource`. Completed lifetime spans are separate traces with a link to construction context. Their open SDK frames are detached immediately, so an unrelated function or task does not inherit a lifetime span. Shutdown stops owned cleanup observations and retains unresolved objects/resources in local health; it does not end them at a fabricated GC or release time. Duplicate tracking is counted separately from loss. Capacity, unsupported bindings/sizes and failed context detachment remain visible as observer failures.

The recorder limits active owned functions to at most 512 and uses the configured bounded lifetime capacity. This does not qualify interaction with arbitrary application-owned SDK context or upstream context-table pressure. The public launcher must still define/refuse unsupported profiles and expose the complete health evidence.

Generated lifetime-only constructor/release hooks use `Watch`, a bounded completion guard with no function span or measurement. Selecting a lifetime therefore does not override the separate function inclusion/exclusion rules. Its observer starts, ends, active count and losses are reported separately. A failed, panicking, exiting or unknown completion cannot authorise a successful factory/release observation.

```sh
make go-check
PYTHONPATH=tests .venv/bin/python -m unittest -v test_go_probe_integration
```

The component checks use real SDK meters/spans, cached collection gates, concurrent starts, startup-off, weak GC, successful/failed/duplicate releases, pending shutdown lifetimes, unknown stacks and instrument-construction errors. The compiler fixture compares actual stock and woven applications with recording enabled/disabled and sampling enabled/disabled, retaining separate counts and SDK/context evidence. The fixture explicitly requests GC; production probes do not. These checks do not establish live public control commands, downstream Collector receipt, benchmarks, broad source coverage or oteleq admission.

## Owned runtime and common protocol

The candidate `runtime` package now connects those probes to its isolated official SDKs, periodic export, the common authenticated loopback control descriptor and bounded flush/shutdown. `go_runtime_config` translates the existing TOML and resolved selections into a private hash-bound plan. Generated package helpers share one embedded SDK in the application PID through `FromEnvironment`; they do not install application-global providers. Existing `OTEL_*` SDK variables are refused before SDK construction instead of being changed or allowed to produce uncontrolled SDK diagnostics. The complete source builder must perform that refusal before starting the application.

Parent capture copies only immutable span context, not an application-owned span object. The official no-op tracer can return its existing non-recording parent directly; supplying that object would let a disabled tracing probe end or mutate it. The regression reproduces those parent mutations before the context-only handoff and checks unchanged parent state afterwards.

The common metric-control client reads the same `control.json` format as the other TCP adapters. `status`, on/off, independent collection/emission, flush and health commands operate without a restart or rebuild. An incomplete client is closed during shutdown. Export failure remains visible after retry; API success does not establish downstream receipt.

A collection-only switch does not wait for an unrelated SDK export; it changes future measurement admission while a previously started export can settle. Explicit emission/off commands keep the strict in-flight export barrier. The stalled-receiver regression checks this separation rather than treating a Collector timeout as a reason to leave collection enabled.

Go health uses explicit schema 4. It carries the actual application/embedded-SDK PID, policy/selection digests, both live switches, raw exporter outcomes, trace queue state, separate function/auxiliary observer accounting, GC/release/pending counts and bounded final flush/shutdown outcomes. `http_spans` separately counts started, ended and active recording SDK spans in the official HTTP instrumentation scope. An incomplete response body remains active after SDK shutdown; a clean exporter queue cannot hide it. Counters use decimal strings. Disabled recording exposes unobserved HTTP/exporter evidence rather than fabricated zero counters. `source_complete=false` and unavailable downstream/partial-rejection evidence remain explicit. Live atomic snapshots are raw observations; consumers need separate quiescent accounting and decoded receipt to establish their own acceptance claims.

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_go_health test_go_runtime_config test_go_runtime_integration
```

The compiler/runtime fixture uses an ordinary interactive application. Four plain/instrumented process pairs cover every initial collection/emission combination and then change all four states in the same instrumented PID. It compares complete application output, failed/panicking release behaviour and formatter counts, checks actual raw metric/span receipt and lifetime construction links, and validates live/final health through the common Python clients. Original/private application sources, the original module and private SDK are checked unchanged. Compiler intermediates stay in an owned temporary directory and are removed with the fixture.

This is an exact-toolchain integration. Broader dependency/build and allocation/release profiles, complete equivalent-workload qualification, final full gates, independent review and oteleq admission remain required. SDK and control implementation does not close the Go language gap on its own.

## Policy-driven private source builds

The `go_source` inspector uses the pinned `golang.org/x/tools/go/packages` loader with the selected Go compiler. Its separate tooling module keeps that parser dependency out of application modules. It records exact parser input bytes, compiler-selected Go/other/embedded inputs, real type identities, named/unnamed result expressions and error nilability. Inspection does not execute application initialisers. Function identities use `module/package.Function` and `module/package.Type.Method`; lifetime identities use `module/package.Type`. Common source globs use paths relative to the original module root. The shared example policy includes `**/*.go` for these module-relative paths and both `close` and `Close` release names.

`go_build.build_project` copies checked original inputs to a private tree, resolves the common selectors and generates rules for the official weaver. Source exclusion takes precedence over function, allocation and release selection. Lifetime selection does not add function spans for otherwise excluded constructors or release methods. Typed `new(T)` and `&T{...}` expressions are wrapped only in the private copy, including nested expressions and package initialisers. The original files have no telemetry imports or hooks. Pointer-receiver release methods require no arguments and either no result or one declared error-compatible result. Selected unsupported bindings fail preparation instead of becoming successful omissions.

The candidate retains actual stock and woven binaries, compiler/weaver/bundle identities, original and SDK inputs, resolved selections, module graphs and command results. Adding the SDK may add dependencies, but cannot upgrade, replace or remove an existing module resolution, change the Go language version, or change toolchain/GODEBUG defaults. An actual older `x/sys` dependency fixture proves that an SDK-induced upgrade is refused before either application binary is compiled. Initial metric flags do not change compile-time selection identity; a second launch can use the same woven binary with metrics off.

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v test_go_source test_go_build
```

These tests exercise an ordinary multi-package application, imported aliases, a generic function, named error returns, independent function/lifetime selection, automatic allocation/release spans, construction links, complete stdout/stderr and untouched original source/module files. Inspector tests also cover concrete/interface/value error results, type aliases, generic receivers and storage-address refusals. The source inspector and Python builder/resolver each have an independent 80% coverage floor.

This remains a native single-module candidate with the fixed Go compiler. Workspaces, vendor trees, local module replacements, generated Cgo application files and pre-existing instrumentation need separate profiles. Addresses of existing storage, tiny objects, generic lifetime types, value-object storage, reflective/external allocations and arbitrary release signatures remain unsupported. The compiler-selected original module is the current source scope; dependencies and whole-program source closure are not established. `source_complete=false` stays explicit. Original-build-system integration, full provider qualification and final protected delivery remain open.

## Build once, prepare and launch without a compiler

Use the public candidate commands from the checkout after installing its locked tooling. The base Go command selects the pinned native Go 1.27.3 toolchain. This profile preserves module language settings and needs an application module compatible with the SDK's Go 1.26 minimum. Build and runtime outputs must be new directories outside the original application inputs.

```sh
bin/quux-otelc go-protocols
bin/quux-otelc --config examples/otelc.toml go-build \
  --root examples/go --output /absolute/path/to/new-go-build

printf '10\nthrow\nlifecycle\ngc\nquit\n' | \
  bin/quux-otelc --config examples/otelc.toml go \
    --build /absolute/path/to/new-go-build \
    --runtime /absolute/path/to/new-go-runtime
```

The example has no telemetry imports or annotations. Its ten-operation checksum is `41284864`. It retains the same seeded 128-step workload as Java, Python and JavaScript/TypeScript, plus ordinary failure/release and explicit fixture-GC commands. Go GC duration starts at a successful typed allocation; it does not claim a language-level constructor or an exact collection time. The resource boundary ends only after a successful selected `Close` method.

`go-build` emits the binary identities and resolved function/type/hook counts. It retains a schema-4 `build.json`, both binaries, original/private input identities, module graphs, tool identities and compiler-selected inventory. Its explicit `runtime_protocols` binds private configuration 2 and health 4. Earlier build formats are refused before launch. The build is separate from launch; no compiler or weaver is invoked by `go`, preparation, command verification or post-run reading.

For an existing application, replace `--root` with its canonical module root and use `--target ./cmd/app` where appropriate. Adjust the common selectors to its existing function/type names. Pass literal application arguments after `--`. Use `--plain` for the stock binary, or `--prepare-only` to emit the fixed recipe without starting an application or SDK. A prepared runtime is single-use; subsequent launches require a new runtime directory, while the same binaries can be reused.

For interactive live controls, launch without piping `quit`, then use another terminal:

```sh
bin/quux-otelc metrics --socket /absolute/path/to/new-go-runtime/control.json status
bin/quux-otelc metrics --socket /absolute/path/to/new-go-runtime/control.json off
bin/quux-otelc metrics --socket /absolute/path/to/new-go-runtime/control.json collection on
bin/quux-otelc metrics --socket /absolute/path/to/new-go-runtime/control.json emission on
bin/quux-otelc health --report /absolute/path/to/new-go-runtime/health.json
```

Use `health --socket .../control.json` while running; `health.json` is the retained final checkpoint. Metric switches do not require a restart or rebuild and do not change tracing. Initial metric flags, service/export settings and trace sampling can differ between launches if the compiled source/context/lifetime selection is unchanged. A mismatched selection, changed binary, changed original compiler input or altered private build product is refused before starting the application. Ordinary non-compiler data such as application log files may change. Source/native file additions require a new build.

## Prepared API and execution records

`otelc.prepared_go.protocols()` describes the explicit `go-source-v1` profile: build 4; private configuration 2; request, plan, prepared, command and execution 1; health 4. Earlier internal build-1/build-2/build-3 records are refused. Rebuild older instrumented binaries before using this runtime contract; metric switches within a running compatible application still require no rebuild or restart. Discovery does not compile anything or establish source closure or provider admission.

Tooling consumers call `prepare(policy, runtime, build_directory=..., plain=..., arguments=(...))`, then `command(runtime)`. The command is a fixed isolated Python supervisor. Start it with `subprocess.Popen(..., start_new_session=True)` and retain the caller's actual PID, supervisor PID and return code. The supervisor starts exactly one prebuilt stock/woven binary. The Go application and embedded SDK share a PID; the supervisor has a separate PID. Standard input/output/error pass through to the application. No startup hooks run in the tooling supervisor.

After exit, `execution(runtime, supervisor_pid=..., parent_pid=..., returncode=...)` independently rechecks the prepared recipe and fixed inputs, joins the caller-observed owner to `go-supervisor-start.json`, `go-application-start.json` and `go-execution.json`, and compares the exact final SDK report. The reader starts no target, invokes no compiler and performs no live flush. Consumers must retain and inspect `failures`; an execution record containing failures is incomplete evidence.

The caller's read-only parent identity may be PID 1, as for a container host. The owned supervisor and application must each have a PID above 1, distinct from one another and the caller. Reader tests model this namespace boundary using completed plain/instrumented records; they do not establish an end-to-end PID-1 container qualification.

The records preserve the application's original exit code separately from the supervisor's instrumentation status. Export failure can fail the instrumentation run while the application returned zero. Abrupt exit or termination preserves its native exit/signal and leaves missing SDK evidence unavailable. Cancellation received before or during child creation is retained and forwarded after its process identity is published. A failed metadata publication triggers bounded termination/kill/reap of the owned child. The outer caller owns cleanup of the supervisor's complete process group, including after a killed supervisor.

Final SDK drain checks require closed providers, matched function/observer completions, no recorded loss, failed exports, pending trace queue or active recording HTTP spans. Uncompleted object/resource observations remain censored counts; the reader does not fabricate release or GC completion. A clean SDK drain is separate from decoded downstream receipt and whole-program/source qualification.

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v test_prepared_go test_go_supervisor test_go_health test_cli
```

The checks use the real public commands and owned native processes. They cover the common unedited example, all four startup states and live switches, retained emission-off measurements, collection-off silence on flush, raw metrics, typed errors, caught panics, formatter preservation, literal arguments, process/exit tampering, source/embedded-input/product/config changes, original abrupt exit and signal forwarding. A separate coverage harness executes the unchanged supervisor source for SDK lifecycle and simulated metadata-disk-full cleanup; it is not evidence for fixed-command provenance or performance. The benchmark candidate adds all five measured states, actual application/supervisor CPU and RSS, and refusal of failed final health or changed evidence. Required final gates, independent review, oteleq qualification and downstream dashboard acceptance remain open.
