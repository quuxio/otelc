# Public Rust object lifetime candidate

<!-- markdownlint-disable MD013 -->

The standalone Rust candidate connects the common schema-3 `[lifetimes]` policy to compiler-owned type discovery, automatic MIR insertion and the official C++ OpenTelemetry SDK. Developers do not edit their application, add annotations, import telemetry packages or supply probes. This profile needs genuine hosted execution, independent review, protected delivery and oteleq qualification before it is considered delivered.

## Build and run an unchanged application

Install and build the exact tools described in the [Rust source guide](rust-source.md). Use new output directories outside the application root and at least 8 GiB free for builds. The ordinary [lifetime example](../examples/rust/LifetimeApp.rs) contains two application types, a move, two distinct zero-sized values, normal drops and caught panic paths. Its `Drop` implementations perform application work; they contain no instrumentation.

```rust
let item = Item { value: command * 7, fail: command == 2 };
let first = Zero;
let second = Zero;
let moved = item;
std::hint::black_box((&moved, &first, &second));
```

The [example configuration](../examples/rust-lifetimes.toml) selects `*Item*` and `*Zero` through the same lifetime selector format used by the other adapters. It enables function traces independently of metric collection and emission.

```sh
bin/quux-otelc --config examples/rust-lifetimes.toml rust-build \
  --root examples/rust --entry LifetimeApp.rs \
  --output build/rust-lifetimes --tools build/rust-tools \
  --sdk-build build/native-launch-sdk

printf '1\n2\n3\n1\n' | bin/quux-otelc \
  --config examples/rust-lifetimes.toml rust-run \
  --build build/rust-lifetimes --runtime /tmp/otelc-rust-lifetimes
```

The example catches two original panics and prints their normal Rust panic diagnostics. Its output reports three application drops per command, including the panicking destructor. Add `--plain` with a new runtime directory to run the retained stock image. Neither lane rebuilds the application. For interactive control, omit `printf`, leave the process running, and use the [common live metric commands](rust-source.md#run-and-change-metric-state) against its `control.json`. Lifetime spans continue while metric collection or emission is disabled, subject to the independent trace sampling policy.

## Build, span and health bindings

Lifetime-enabled builds use `rust1981-standalone-drop-v2`, build schema 2, native plan 3 and health schema 5. Function-only builds keep their original schema/profile. A build retains ordinary function inspection, version-two lifetime discovery, complete original MIR body fingerprints and the selected function/local/type bindings. Weaving must reproduce those bindings and original declarations. Changed source, selectors, compiler inputs, products or retained records refuse replay. Metric startup flags and lifetime capacity are runtime settings; changing the compiled type selection requires a new build.

The compiler observes successful supported aggregate/constant construction, transfers an address-independent observation key across whole-local moves, and closes the same lifetime at drop entry. Normal and unwind completions are retained separately. Zero-sized values remain distinct. A lifetime is a separate root span, linked to the current function span when one is present, rather than a child that forces the function to remain active. A Rust drop span is neither a GC observation nor proof that a named resource `close` method was called. The common `release_methods` list does not redefine Rust drop semantics.

Final `health.json` retains admitted, completed, live, dropping, ending, censored, lost, duplicate and invalid observations. The launcher validates the actual process, policy, compiled selection and matching health profile. It retains health even when capacity prevents complete observation; health validation alone is not a loss-free or downstream-delivery claim. Benchmark publication additionally requires complete, closed, uncensored drop accounting, zero observation loss, complete SDK export accounting and successful shutdown.

## Shared benchmark policy

The existing `WorkItem` in [BenchmarkApp.rs](../examples/rust/BenchmarkApp.rs) needs no source changes. To measure installed function and drop probes, use `examples/otelc.toml` in both the `rust-build` and `benchmark --language rust` commands from the [Rust benchmark guide](rust-benchmarks.md). Its source selector assumes `--root "$PWD" --entry examples/rust/BenchmarkApp.rs`. All four live metric states run in one instrumented process and are compared with the stock executable. Use the same policy and comparison ID for other languages. The metrics-only policy remains available for a separate comparison without lifetime probes. Retain the policy with every timing result; the two experiments measure different enabled work.

## Checks and remaining boundaries

`make rust-public-check` includes the ordinary lifetime example, both CLI lanes, stock output/drop/panic comparisons, all four startup metric combinations, same-PID switches, compiler-free replay, decoded function/lifetime links, actual drop-unwind outcomes, foreign-type refusal and a stock/four-state lifetime benchmark smoke case. A separate public capacity case requires a one-object limit to report two lost observations without changing application output; traces-off must retain complete drop bookkeeping without exporting spans. Per-component Python coverage floors remain at 80%. The separate compiler/SDK gate retains its own Rust coverage floors. Hosted evidence excludes regenerable build trees and retains token-free plan settings, compiler records, final health, decoded telemetry and raw benchmark samples. A short smoke case does not establish performance or downstream Grafana receipt.

The supported boundary is a concrete original-application ADT with drop glue in an ordinary synchronous body, tracked as a whole root local. Selected arguments/returns, escaping ownership, generic/unavailable layouts, non-drop types and unsupported ownership operations refuse compilation. Selected foreign ADTs and owned arrays/tuples/opaque values refuse before weaving. Empty lifetime matches also refuse. Broader field/container ownership, transfers through calls/returns, pinning, async/cancellation, Cargo/build scripts, whole-program dependency closure and prepared pre-exec launch remain work. Skipped compiler bodies and incomplete source/instrumentation coverage stay explicit; this is not exhaustive lifetime instrumentation or whole-language admission.
