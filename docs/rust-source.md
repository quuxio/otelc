# Original-source Rust candidate

<!-- markdownlint-disable MD013 -->

`rust-build` retains stock and instrumented executables from an unchanged standalone Rust entry and its modules. The private compiler inserts typed synchronous MIR probes, including unwind cleanup, and links the existing official C++ OpenTelemetry SDK. Developers add no telemetry imports, annotations, guards or bootstrap. The common schema-3 policy selects sources and functions. Rust remains a candidate in `plan`, not a completed language adapter.

## Build the tools and example

Use Python 3.12+ with the repository's locked dependencies, Rust **1.98.1** with its matching `rustc-dev` component and LLVM **22.1.8**. The native SDK also requires the tools in the [native launch guide](native-launch.md#build-and-run). Actual compiler/SDK execution has been checked on macOS ARM64. GNU/Linux x86-64 and ARM64 are accepted host profiles; their hosted checks must pass before they are described as verified. Compiler-private interfaces require this exact Rust version.

Run from the repository root. Tool and application builds require at least 8 GiB free space and retain failed attempts rather than deleting their diagnostics. Use new output directories for each build.

```sh
make setup
rustup toolchain install 1.98.1 --profile minimal --component rustc-dev,clippy,rustfmt
rustup run 1.98.1 cargo fetch --locked --manifest-path native/rust/compiler/Cargo.toml
make native-launch-sdk rust-tools
make stack-up

bin/quux-otelc --config examples/functions.toml rust-build \
  --root examples/rust --entry BenchmarkApp.rs \
  --output build/rust-example --tools build/rust-tools \
  --sdk-build build/native-launch-sdk --optimisation 2
```

The compiler and bridge use fixed offline, locked Cargo recipes. Cargo dependency fetching above is a separate preparation step. The build records original inputs, compiler/bridge/SDK identities, compiler inventories, selectors and both application products. Readers recheck them before launch and after normal execution; this does not establish complete dependency or source-origin closure. An existing SDK must match the current checkout's native sources; do not repair or relocate old build records to make them pass.

Each compiler command retains its arguments, working directory, stdout, stderr, process exit and elapsed time. A failed command also includes its exit code and the last 4096 stderr bytes, decoded with replacement and escaped, in the raised error. This preserves useful diagnostics if a temporary test directory is later removed; the complete original stderr remains the authoritative channel while retained. The compiler link recipe searches the selected Rust sysroot's library directory explicitly for its matching LLVM library, in addition to retaining that directory as a runtime search path. The focused `Rust public compiler tools` workflow exercises the exact public tool builder and original-source inspection on Linux and macOS without an SDK build or application execution. Its explicit Bash shell preserves failing test exits through log pipelines. It retains small command/diagnostic records for 14 days, excluding regenerable Cargo build trees. These focused checks do not replace the full runtime, coverage or protected delivery gates.

`examples/rust/BenchmarkApp.rs` contains ordinary application code. Its selected function performs the same 128-step integer workload as the other language examples:

```rust
fn workload(seed: i32) -> u64 {
    assert!(seed >= 0, "negative seed");
    let item = WorkItem { seed };
    let mut value = item.seed as u64;
    let mut sum = 0;
    for _ in 0..128 {
        value = (value * 1664525 + 1013904223) & 65535;
        sum += value;
    }
    sum
}
```

The example reads a batch count from standard input, writes one JSON result with checksum, calls and elapsed nanoseconds, and exits on `quit`. Its ordinary `WorkItem` and `Drop` implementation are not lifetime instrumentation. Automatic object/drop tracing is still unsupported in this Rust profile.

`examples/functions.toml` applies unchanged across all eight language targets. It selects `*workload*`, enables sampled function traces at ratio 1 and disables lifetimes. The default context switches are off. The broader lifetime/context policy `examples/otelc.toml` is refused by this Rust candidate until those capabilities exist.

## Run and change metric state

Start an interactive instrumented process:

```sh
bin/quux-otelc --config examples/functions.toml rust-run \
  --build build/rust-example --runtime /tmp/otelc-rust-example-run
```

Enter `10` to perform a batch. Leave that process running and use a second terminal:

```sh
bin/quux-otelc metrics --socket /tmp/otelc-rust-example-run/control.json status
bin/quux-otelc metrics --socket /tmp/otelc-rust-example-run/control.json collection off
bin/quux-otelc metrics --socket /tmp/otelc-rust-example-run/control.json emission off
bin/quux-otelc metrics --socket /tmp/otelc-rust-example-run/control.json collection on
bin/quux-otelc metrics --socket /tmp/otelc-rust-example-run/control.json emission on
```

Send another batch between transitions. Collection and emission are independent: collection-off prevents new metric observations, while emission-off prevents SDK metric export and retains collected cumulative measurements for restoration. Neither switch disables function traces. Startup-off also permits later enable without rebuilding or restarting; set the two `[metrics]` booleans in the same policy before launch.

Enter `quit` for orderly shutdown and export. Inspect the retained final SDK checkpoint:

```sh
bin/quux-otelc health --report /tmp/otelc-rust-example-run/health.json
```

The direct launcher checks that final health identifies the actual child, Rust language, policy and selection with the SDK attached. It retains `rust-execution.json`, including the original process exit and any validation failure. A zero-exit application with missing or mismatched SDK health produces launcher exit 1. Non-zero application exits remain non-zero; interrupted waits terminate and reap the owned direct child. Final health alone does not prove downstream receipt.

Run the stock lane using the same build and a new runtime directory:

```sh
bin/quux-otelc --config examples/functions.toml rust-run \
  --build build/rust-example --runtime /tmp/otelc-rust-example-plain --plain
```

Use `--` before literal application arguments. Each runtime directory must be new and outside both the original application root and retained build. Output boundaries compare existing ancestor identities as well as resolved path spelling, so case aliases on case-insensitive filesystems cannot place generated files inside original inputs. Application streams remain unchanged. Ordinary runtime environment settings such as `RUST_BACKTRACE` are retained; inherited telemetry settings do not replace the common policy.

## Telemetry, checks and limits

The [local stack](benchmarks.md#run-equivalent-language-comparisons) listens on OTLP/HTTP port 4320 and Grafana port 3001. The function-only example uses service `otelc-function-examples` and language `rust`; filter those labels in the instrumentation dashboard or select its service in the trace dashboard. Actual Rust function spans and call metrics have reached Collector, Tempo, Prometheus and Grafana using this candidate's modular test fixture. This is functional evidence, not a cross-language performance ranking or qualification of the example commands above.

`make rust-public-check` runs real public build/launch tests and enforces separate 80% Python coverage floors for compiler tools, application building and direct launch. Tests cover source preservation, original modules/panics/drop side effects, all four metric startup states with same-process live switches, decoded OTLP, literal arguments, missing SDK health, signals and interrupted-child cleanup. The separate `make rust-compiler-check` exercises compiler/MIR and native SDK integration; both also belong to the full repository gate.

This profile is standalone, edition 2024, optimisation 0..3 and synchronous named functions. Selected async, macro-expanded and closure bodies refuse. Cargo/build scripts, `env!` inputs, arbitrary compiler flags, custom targets, prepared pre-exec release, whole-program source/dependency closure, automatic object/resource lifetimes, task/service propagation and equivalent paired Rust benchmarks remain unsupported. Full hosted checks, independent review and oteleq admission are still required before declaring the language complete.
