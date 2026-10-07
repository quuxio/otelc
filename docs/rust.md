# Rust instrumentation 101

The Rust adapter parses original Rust syntax and inserts body guards into a private source tree. Standalone files use rustc; Cargo projects use a compiler wrapper. Application source, Cargo manifests and lockfiles stay unchanged. The linked Rust OpenTelemetry SDK supplies cumulative function counters and duration histograms. No application SDK import or required annotation is needed.

## Build and run

Use the qualified Rust 1.98.1 toolchain. The probe library and application must use the same compiler, host target and compatible panic strategy. Build from the repository root and start the [Collector, Prometheus and Grafana stack](observability-stack.md):

```sh
make build
./target/debug/quux-otelc --config examples/rust.toml --language rust doctor
./target/debug/quux-otelc --config examples/rust.toml --language rust inspect examples/apps/rust_app.rs --json
rustc --edition=2024 -O -g examples/apps/rust_app.rs -o /tmp/plain-rust-app
/tmp/plain-rust-app
./target/debug/quux-otelc --config examples/rust.toml rust examples/apps/rust_app.rs
```

Both applications print `72`. Rust's normal panic hook also prints the two caught panic diagnostics. Instrumentation records ten calls: recursion, receiver methods, an escaping panic, an internally caught panic and thread work. The escaping payload remains the original `String`; internally caught panics do not become escaping-unwind observations.

From an existing Cargo project, supply an absolute CLI and configuration path:

```sh
/Users/sclarke/github/otelc/target/debug/quux-otelc \
  --config /path/to/project/otelc.toml rust Cargo.toml --bin server -- APP_ARGS
```

An existing lockfile is required. Cargo keeps the original manifests, dependency resolution, build scripts and compiler flags. Only project Rust compiler inputs are substituted; registry and external dependency sources remain untouched. Build artefacts and generated inputs are temporary. Multiple binary targets require `--bin NAME`. Application arguments and exit codes pass through.

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

Private generated compiler input resembles:

```rust
fn annotated(value: i32) -> i32 {
    /*otelc.instrument*/
    let __quux_otelc_guard = ::quux_otelc_rust::enter("examples.apps.rust_annotated.annotated");
    value * 3
}
```

The actual insertion adds no new lines. Bindings are chosen against original identifiers, including macro token groups. The original function body and signature remain intact. Guards run at normal return and panic unwinding; guards entered while an existing panic unwinds record normal cleanup completion. Body-local destructors run before the guard. Parameter destruction after the body is outside this duration. Original `file!()` and `line!()` locations are retained through compiler path remapping; inserted code changes columns on the insertion line. A separate main-body guard closes the SDK, even when main timing is excluded. Generated metadata is optional; generated inputs are rejected on a second instrumentation pass.

## Live metrics and latency

The service is `otelc-rust-example`. Calls, escaping unwinds, seconds histograms, observation losses and export losses travel through the Collector to the existing Grafana dashboard. Common resources, explicit histogram bounds, endpoint/header precedence, capacities and timeouts apply.

Configure `runtime.control_socket` inside an owner-only directory and use the shared `status`, `enable` and `disable` commands from the [developer guide](developer-101.md). The owner-only socket reports the actual application PID. Disabled admission leaves existing tokens intact; enabling cannot retrospectively measure calls started while disabled. The application keeps running without rebuild or restart. Cumulative history remains available, and unchanged snapshots are suppressed after draining.

```sh
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/rust --iterations 10000 --runs 8"
make rust-check rust-coverage
```

The current `make build` SDK is a debug build, so these results include debug SDK costs. The benchmark compiles the unchanged baseline and instrumented application with `--edition=2024 -O -g`, warms both and alternates off/on in one instrumented PID. Compilation, launch, control requests and shutdown are outside body timings. Reports retain hashes, checksums, all samples, exact calls and complete-export/zero-loss evidence. Rust adapter and Rust probe product coverage each have an independent 80% line gate, as well as the existing native aggregate gate.

## Async functions and cancellation

Named async functions and methods use the same external selection policy. An observation starts on the first poll, ends at completion, escaping panic or cancellation, and includes time suspended between polls. Constructing and dropping an unpolled future produces no observation. Cancellation records a call and duration plus `otelc.function.cancellations`; it is separate from escaping panic unwinding and observation loss. A cancelled future's ordinary cleanup still runs. Disabling metrics affects admission at the first poll; an already admitted future retains its token through later polls and destruction. Shutdown reports still-pending futures as incomplete.

```sh
rustc --edition=2024 -O examples/apps/rust_async_app.rs -o /tmp/plain-rust-async
/tmp/plain-rust-async
./target/debug/quux-otelc --config examples/rust-async.toml rust examples/apps/rust_async_app.rs
```

Both runs print the original destructor order `["local", "second", "first"]` and `async results preserved; cleanups=2`. The instrumented run records 14 calls, two cancellations and one escaping unwind. The fixture covers borrowed outputs, mutable receivers/parameters, generic non-Send values, opaque outputs, return coercions, early returns, `?`, preserved panic payloads, cancellation and movement between threads. No executor dependency or original-source change is required.

Generated input for a simple async function resembles:

```rust
async fn ready(value: i32) -> i32 {
    ::quux_otelc_rust::observe_future("examples.apps.rust_async_app.ready", async move {
        let value = value;
        ::std::convert::identity::<i32>(value + 1)
    }).await
}
```

The native await polls the original body directly without an additional scheduled task. Parameter shadowing preserves reverse parameter destruction order; concrete return coercions retain the original expected output type. This changes the compiler-generated future's layout and adds runtime state, but preserves the qualified results and cleanup behaviour. Async destructured, wildcard and `ref` parameters are rejected visibly pending compiler-level lifetime qualification. Executor attribute macros controlling async `main`, per-poll CPU timing and complete macro-expansion coverage remain unsupported. The paired async benchmark measures immediately completing futures; it is not evidence for every executor or suspended workload. Run `make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS="--rust-async --output build/benchmarks/rust-async --iterations 10000 --runs 8"` to compare the unchanged baseline with metrics off/on in one instrumented PID. It verifies source hashes, matching checksums, exact calls, complete export and zero losses.

## Current boundaries

This milestone supports synchronous and the qualified async named functions, methods, trait default methods, generics, nested functions, normal OS threads and panic unwinding on the qualified macOS ARM64 host. Const and naked functions are rejected when selected; async main is rejected. Function metrics for closures and macro-generated items are not implemented. Selected `include!` source fragments, test/procedural-macro/edition-2015 crates and existing compiler wrappers require separate qualification. Normal expression macros remain usable. Symlinked source modules require separate qualification. Build scripts execute unchanged and do not start instrumentation.

Rust `panic=abort`, `process::exit`, forced termination and unjoined background work cannot guarantee final observations or export. Custom targets, cross-compilation, sanitizers and altered panic strategies require matching SDK qualification. Automatic value lifetime/move/drop metrics, distributed context and spans remain unavailable and are rejected by configuration. Timing a `Drop::drop` method measures that method body, not an object's entire lifetime.

The pinned SDK's custom-reader API is experimental in 0.32.1. A bounded worker pulls cumulative SDK snapshots and uses the existing OTLP/HTTP transport, with one total retry deadline, no redirects and at most 64 KiB of acknowledgement data. Malformed/partial acknowledgements and failed exports remain explicit losses. SDK aggregation and protobuf encoding stay language-native; the shared transport does not replace the SDK. Final shutdown is bounded by the common deadline; a Collector outage does not delay the application indefinitely.
