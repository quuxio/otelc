# Rust performance comparison candidate

<!-- markdownlint-disable MD013 -->

This candidate compares the unchanged [Rust example](../examples/rust/BenchmarkApp.rs) against equivalent Java, Microsoft TypeScript/JavaScript, Python, Go and C/C++ workloads. It requires a retained [original-source Rust build](rust-source.md), Rust 1.98.1 and the matching native SDK. It does not add Cargo, async, lifetime or task/service support. Actual end-to-end execution, independent review, required hosted checks and downstream Grafana evidence remain acceptance requirements.

## Build and run

Use the same [metrics comparison policy](../examples/benchmark-metrics.toml) for every language. It selects `workload`, excludes `excluded`, disables lifetimes and uses the same integer128 workload. Developers add no telemetry code. Build outputs and benchmark results must be outside the original source root and outside each other's retained products, including physical directory aliases. Existing output directories are refused.

From the otelc checkout, after `make setup` and installing the pinned tools described in the Rust build guide:

```sh
make stack-up
run=$(mktemp -d /tmp/otelc-rust-comparison.XXXXXX)
make native-launch-sdk NATIVE_LAUNCH_OUTPUT="$run/sdk"
make rust-tools RUST_COMPILER_OUTPUT="$run/tools"
bin/quux-otelc --config examples/benchmark-metrics.toml rust-build \
  --root "$PWD" --entry examples/rust/BenchmarkApp.rs \
  --tools "$run/tools" --sdk-build "$run/sdk" --output "$run/compiled"
bin/quux-otelc --config examples/benchmark-metrics.toml benchmark \
  --language rust --build "$run/compiled" --output "$run/results" \
  --comparison-id integer128-comparison
```

The equivalent Make invocation is:

```sh
make benchmark BENCHMARK_LANGUAGE=rust \
  BENCHMARK_CONFIG=examples/benchmark-metrics.toml \
  BENCHMARK_BUILD="$run/compiled" BENCHMARK_OUTPUT="$run/results-second" \
  BENCHMARK_COMPARISON_ID=integer128-comparison
```

Use the same comparison ID when launching the other language benchmarks as described in the [comparison guide](benchmarks.md). Use `--no-publish` for a local report without exporting driver measurements; application telemetry still follows the policy endpoint. Benchmarking does not rebuild either executable. The retained build must contain the exact ordinary example with a non-empty function selection; an arbitrary program with a similar output protocol is refused.

## Measurements and failure behaviour

The plain lane directly executes the stock binary without SDK, probes, bootstrap or supervisor. The instrumented lane directly executes the woven binary with its embedded C++ SDK. Both lanes retain startup-to-first-response separately from workload-body latency. Warmup, controls, OS observations and explicit SDK flushes are outside the application timer. CPU windows include the batch and flush. RSS is the larger of two OS observations, not a peak-allocation measurement. The SDK is counted once with the application; there is no separate SDK or supervisor process sample.

One instrumented PID runs all four collection/emission states. The schedule balances state positions and reverses alternate blocks. After the final sample, both gates are restored and collected values are flushed outside measurement windows. Startup-off policies can enable in the same process. Tracing retains its configured behaviour throughout; metric-off timing includes any installed probes and enabled trace work.

The driver checks independent checksums, unchanged build/source/product identities and final SDK health bound to the actual Rust PID, policy and selection. Active/lost/invalid function observations, pending/dropped spans, failed exports, incomplete SDK shutdown or unavailable counters refuse a successful report and publication. SDK counters do not establish downstream receipt. Raw responses, samples, controls, stderr and final health remain available after a failure. Cleanup attempts stop/reap, input verification and final SDK validation independently, preserving the first error and attaching later failures as notes. Exported measurements cannot be rolled back if a later exporter shutdown fails.

This direct-child profile does not qualify prepared host release, arbitrary child processes or complete source/dependency provenance. `source_complete=false` remains explicit. Rust ownership/drop and async cancellation remain required project work; they are not inferred from these function benchmarks.

## Grafana

Open [language performance comparison](http://localhost:3001/d/otelc-comparison) and choose `integer128-comparison`. The existing queries group by language and require no Rust-specific synthetic series. Compare absolute ns/operation and throughput, each language's overhead relative to its own stock baseline, CPU/operation and RSS. Keep runtime/compiler flags and trace/lifetime policy with each result. A small correctness run is not a reliable performance estimate. Verify actual Prometheus series and decoded Tempo receipt separately before claiming a demonstrated comparison.

## Checks

`make rust-public-check` runs the literal refusal/cleanup checks and genuine public Rust execution tests, including the maintained benchmark example, all four startup metric combinations, direct comparison and CLI dispatch. It requires at least 80% coverage separately for the Rust compiler/build/launch/benchmark modules and shared cleanup helper. Genuine tests require 8 GiB free before fresh compiler/SDK construction; a low-disk refusal is not a passing test. `make check` remains the full protected-delivery gate.
