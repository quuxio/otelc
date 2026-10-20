# Rust instrumentation 101

The default `compiler` backend inserts probes into the pinned compiler's typed MIR while reading original Rust input. Standalone files and Cargo projects use the same helper. Application and dependency source, Cargo manifests, lockfiles, original source columns and tracked caller locations remain unchanged. Linked read-only modules keep their configured project-relative identities; the helper also checks their physical source hashes and SDK exclusions.

The linked Rust OpenTelemetry SDK supplies cumulative function counters, duration histograms and spans. No application SDK import or required annotation is needed. Compiler instrumentation adds no fields or owning guard to an application's future or closure.

An explicit [experimental LLVM profile](rust-compiler.md) instruments original synchronous standalone input without source insertion. It requires a separate pinned nightly SDK/pass pairing and remains under qualification. Its async and Cargo paths are unavailable; the default backend below retains its separately qualified support.

Function spans are available through the same selection policy; see [Rust spans](rust-spans.md) for unchanged examples, sampling, parents, loss handling and the Tempo viewer.

## Build and run

Use the qualified macOS ARM64 Rust 1.98.1 toolchain, its `rustc-dev` component and LLVM 22.1.8. `make build` produces the stable compiler helper and its fingerprinted profile; bootstrap permission applies only while building that private helper, never while compiling applications. The probe library and application must use the same compiler, host target and compatible panic strategy. Build from the repository root and start the [Collector, Prometheus and Grafana stack](observability-stack.md):

```sh
rustup component add --toolchain 1.98.1 rustc-dev
make build
./target/debug/quux-otelc --config examples/rust.toml --language rust doctor
./target/debug/quux-otelc --config examples/rust.toml --language rust inspect examples/apps/rust_app.rs --json
rustc --edition=2024 -O -g examples/apps/rust_app.rs -o /tmp/plain-rust-app
/tmp/plain-rust-app
./target/debug/quux-otelc --config examples/rust.toml rust examples/apps/rust_app.rs
```

Both applications print `72`. Rust's normal panic hook also prints the two caught panic diagnostics. Instrumentation records 15 calls: recursion, receiver methods, an escaping panic, an internally caught panic, parsed closures and thread work. Two escaping-unwind observations include the outer escaping function and the closure that panics inside the catching function. The escaping payload remains the original `String`; internally caught panics do not become escaping-unwind observations.

## Build once and launch repeatedly

For standalone sources, keep compiler diagnostics separate from application output:

```sh
mkdir -p build
./target/debug/quux-otelc --config examples/rust.toml rust \
  --build examples/apps/rust_app.rs --output-dir build/rust-example \
  >build/rust-build.stdout 2>build/rust-build.stderr
./target/debug/quux-otelc --config examples/rust.toml rust --run build/rust-example
./target/debug/quux-otelc --config examples/rust.toml rust --run build/rust-example
```

Build-only compiles without executing the application and refuses an existing output directory. The private schema-3 bundle contains `application`, the retained resolved `plan.json`, `build.json`, `profile.json`, `compiler-request.json` and the actual compiler-owned `inventory.json`. Separate `compiler_driver` evidence binds the helper, profile, request and inventory fingerprints; it does not reuse the LLVM plugin identity. The manifest records SHA-256 fingerprints for the executable, policy, original entry source, SDK and adapter, plus the actual compiler program/argv/version and the baseline flags `--edition=2024 -O -g`. Use those same flags for the ordinary build when comparing runtime latency. Compiler warnings remain in the build's original channels.

`--run DIRECTORY [APP_ARGS...]` verifies the executable/policy pair and exact matching adapter before launching. It invokes no compiler and needs neither original source nor an SDK rlib. It resolves the supplied common configuration again: runtime metrics, traces, export endpoints and resource settings can change, while build-time source/function selectors, backend and annotation settings must match. A changed build-time policy requires a new bundle. Application stdout/stderr and exit status belong to the application run; fatal termination still cannot guarantee export.

The manifest is written last; incomplete, malformed or mismatched bundles are rejected. The bundle is tied to its qualified adapter binary, so retain that adapter for replay or rebuild after changing it. Build-only Cargo projects and general cross-host redistribution still require qualification; the existing combined Cargo route below is unchanged.

## Cargo launch

From an existing Cargo project, supply an absolute CLI and configuration path:

```sh
/Users/sclarke/github/otelc/target/debug/quux-otelc \
  --config /path/to/project/otelc.toml rust Cargo.toml --bin server -- APP_ARGS
```

An existing lockfile is required. The manifest must be inside the current project directory; an outside manifest is refused before Cargo builds or runs the application. Cargo keeps the original manifests, dependency resolution, build scripts and compiler flags. Project crates are compiled from their original paths with compiler-owned probes; registry and external dependency sources remain untouched. Build artefacts and compiler-request records are temporary. Cargo compilation diagnostics remain on stderr before application output. Multiple binary targets require `--bin NAME`. Application arguments and exit codes pass through.

Compact async bodies, including a return immediately before the closing brace, preserve their original delimiters and concrete return coercions. The [compact async fixture](../examples/apps/rust_async_compact.rs) covers primitive, borrowed, function-pointer and trait-object results, empty bodies and explicit returns.

## Source, policy and optional annotations

An ordinary function remains ordinary Rust:

```rust
fn process_order(value: i32) -> i32 { value * 3 }
```

Select it in the common policy:

```toml
schema_version = 2
languages = ["rust"]
[sources]
include = ["examples/apps/rust_*.rs"]
[functions]
include = ["examples.apps.rust_app.process_order"]
[adapters.rust]
backend = "compiler"
```

Identities use the original project-relative file path without `.rs`, followed by inline modules and the named function, receiver method or trait method. Examples are `examples.apps.rust_app.Order.calculate` and `main.<Cleanup as Drop>.drop`. Generic functions retain one source identity across monomorphisations. Inspection reports exact identities and unsupported function kinds. Common byte-glob exclusions take precedence.

Optional adjacent line comments and Rust doc comments are read when `annotations.read_existing = true`:

```rust
// otelc.instrument
fn annotated(value: i32) -> i32 { value * 3 }

fn configured(value: i32) -> i32 { value * 3 }

// otelc.exclude
fn excluded(value: i32) -> i32 { value * 3 }
```

```sh
./target/debug/quux-otelc --config examples/rust.toml rust examples/apps/rust_annotated.rs
```

It prints `30` and records two calls. Unknown instrumentation comments fail. A blank line breaks ordinary comment association. Required custom procedural attributes are unnecessary.

The source shown above is also the source read by the instrumented compiler. Probe entry, normal return and unwind operations are inserted into typed MIR, after source parsing and macro expansion. Original `file!()`, `line!()`, `column!()` and tracked caller locations are preserved. A compiler-inserted main boundary starts and closes the SDK even when main timing is excluded. Exit observations include cleanup inside the original function's MIR; later destruction of a closure object is a separate boundary.

Inspection uses the same compilation path in a private temporary directory. Its schema-3 inventory records original declarations, compiler names, actual emitted symbols, source hashes and selection. It never executes `main` or exports telemetry. Compilation and a matching compiler profile are required. Configured insertion does not create annotated application files; existing adjacent annotations continue to guide selection.

## Live metrics and latency

The service is `otelc-rust-example`. Calls, escaping unwinds, seconds histograms, observation losses and export losses travel through the Collector to the existing Grafana dashboard. Common resources, explicit histogram bounds, endpoint/header precedence, capacities and timeouts apply.

Configure `runtime.control_socket` inside an owner-only directory and use the shared `status`, `enable` and `disable` commands from the [developer guide](developer-101.md). The owner-only socket reports the actual application PID. Disabled admission leaves existing tokens intact; enabling cannot retrospectively measure calls started while disabled. The application keeps running without rebuild or restart. Cumulative history remains available, and unchanged snapshots are suppressed after draining.

```sh
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/rust --iterations 10000 --runs 8"
make rust-check rust-coverage
```

The current `make build` SDK is a debug build, so these results include debug SDK costs. The benchmark compiles the unchanged baseline and instrumented application with `--edition=2024 -O -g`, warms both and alternates off/on in one instrumented PID. Compilation, launch, control requests and shutdown are outside body timings. Reports retain hashes, checksums, all samples, exact calls and complete-export/zero-loss evidence. Rust adapter and Rust probe product coverage each have an independent 80% line gate, as well as the existing native aggregate gate.

## Async functions and cancellation

Named async functions and methods use the same external selection policy. An observation starts on the first poll, ends at completion, escaping panic or cancellation, and includes time suspended between polls. Constructing and dropping an unpolled future produces no observation. Cancellation records a call and duration plus `otelc.function.cancellations`; it is separate from escaping panic unwinding and observation loss. A cancelled future's ordinary cleanup still runs. Dropping a suspended future during an unrelated caller panic remains cancellation. A panic escaping a poll or the future's destructor records an unwind, including a nested panic caught during another unwind; the original panic payload and caller context are preserved. Disabling metrics affects admission at the first poll; an already admitted future retains its token through later polls and destruction. Shutdown reports still-pending futures as incomplete.

```sh
rustc --edition=2024 -O examples/apps/rust_async_app.rs -o /tmp/plain-rust-async
/tmp/plain-rust-async
./target/debug/quux-otelc --config examples/rust-async.toml rust examples/apps/rust_async_app.rs
```

Both runs print the original destructor order `["local", "second", "first"]` and `async results preserved; cleanups=2`. The instrumented run records 14 calls, two cancellations and one escaping unwind. The fixture covers borrowed outputs, mutable receivers/parameters, generic non-Send values, opaque outputs, return coercions, early returns, `?`, preserved panic payloads, cancellation and movement between threads. No executor dependency or original-source change is required.

Poll and drop hooks operate after the compiler has fixed the original future layout. A bounded external registry tracks admission and completion without retaining an application reference, changing the future's fields or adding an owning guard. Original future size, alignment, `needs_drop`, `Send` behaviour, parameter destruction and cancellation cleanup are exercised by the paired compiler fixtures. Capacity exhaustion rejects unknown future identities consistently, and shutdown reports remaining work as incomplete.

Executor attribute macros controlling async `main`, per-poll CPU timing and executor/task context propagation require separate qualification. The paired async benchmark measures immediately completing futures; it is not evidence for every executor or suspended workload. Run `make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS="--rust-async --output build/benchmarks/rust-async --iterations 10000 --runs 8"` to compare the unchanged baseline with metrics off/on in one instrumented PID.

## Synchronous closures

Synchronous closures receive compiler body probes without replacing the closure object, changing its captures or adding fields. Both expression and block bodies are supported. Identities include enclosing function/closure scopes and the original opening pipe's line/column, for example `examples.apps.rust_closure_app.main.<closure>@10:18`. Nested and same-line closures have distinct identities. Editing source positions changes these identities; use `inspect` before selecting particular callbacks.

```sh
rustc --edition=2024 -O examples/apps/rust_closure_app.rs -o /tmp/plain-rust-closures
/tmp/plain-rust-closures
./target/debug/quux-otelc --config examples/rust-closures.toml rust examples/apps/rust_closure_app.rs
./target/debug/quux-otelc --config examples/rust-closures.toml rust examples/apps/rust_closure_annotated.rs
```

The first pair preserves results and prints `closure results preserved; drops=["second", "first", "capture", "local", "argument"]`. The configured run measures 20 closure invocations and one escaping panic with no observation loss. The fixture qualifies `Fn`, `FnMut`, `FnOnce`, owned and borrowed results, early returns, destructured inputs, function-pointer coercion, a noncapturing closure returned by a const factory, disjoint field capture, nested closures and thread movement. An uncalled closure produces no observation; creating and discarding a future from a synchronous closure records one body call and no future cancellation.

The annotated example prints `30` and records two calls. An adjacent comment can select or exclude a closure bound by `let`, without a runtime import:

```rust
// otelc.instrument
let selected = |value| value * 3;
// otelc.exclude
let excluded = |value| value - 1;
```

External selection still has exclusion priority. Normal return, early return and panic unwinding end the compiler body observation. Closure captures dropped when the closure object itself is destroyed are outside this duration; function timing does not establish automatic object lifetime tracing. A synchronous closure returning a future measures creation of that future, rather than its polling or completion. Async/const closures remain unqualified.

Expanded synchronous declarations and expression-macro callbacks use compiler-owned identities. Where an expanded declaration has no original source alias, inspect the compiled inventory before choosing an exact selector. Uninstantiated generic declarations can remain `emitted=false`; their presence in the declaration inventory does not prove execution.

Measure the unchanged closure workload with live controls:

```sh
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS="--rust-closures --output build/benchmarks/rust-closures --iterations 10000 --runs 8"
```

The baseline and off/on runs use the same closure, compiler flags, source hash and checksums. A complete report requires exact call counts, zero loss, final export and one instrumented PID across live toggles.

## Current boundaries

The qualified profile targets synchronous and named async functions, methods, trait default methods, generics, nested functions, synchronous closures, OS threads and panic unwinding on macOS ARM64. Runtime bodies of `const fn` can be selected without instrumenting compile-time evaluation. Source aliases preserve read-only linked modules. Naked functions, async main, async/const closures, test/procedural-macro/edition-2015 crates and existing compiler wrappers require separate qualification. Build scripts execute unchanged and do not start instrumentation. Standard `GlobalAlloc` methods and selected helpers called within an allocator's dynamic scope suppress recording to prevent allocator re-entry; an unrelated application trait with the same spelling remains eligible.

Rust `panic=abort`, `process::exit`, forced termination and unjoined background work cannot guarantee final observations or export. Custom targets, cross-compilation, sanitizers and altered panic strategies require matching SDK qualification. Automatic value lifetime/move/drop metrics and distributed context remain unavailable. Function spans are supported within the [qualified Rust boundaries](rust-spans.md); automatic value lifetimes remain rejected by configuration. Timing a `Drop::drop` method measures that method body, not an object's entire lifetime.

The pinned SDK's custom-reader API is experimental in 0.33.1. A bounded worker pulls cumulative SDK snapshots and uses the existing OTLP/HTTP transport, with one total retry deadline, no redirects and at most 64 KiB of acknowledgement data. Malformed/partial acknowledgements and failed exports remain explicit losses. SDK aggregation and protobuf encoding stay language-native; the shared transport does not replace the SDK. Final shutdown is bounded by the common deadline; a Collector outage does not delay the application indefinitely.
