# Plain versus instrumented benchmarks

## Run the comparison

The benchmark uses the internal deterministic CPU workload in `tests/fixtures/benchmark.cpp`. No external repository or network workload is required. Build requirements are Rust and the matched Homebrew LLVM 22 toolchain; start the [Docker metrics stack](observability-stack.md) to verify export as well.

```sh
make stack-up
make benchmark
# More repetitions or threads:
make benchmark BENCHMARK_ARGS='--runs 12 --iterations 100000 --threads 4 --output build/benchmarks-4threads'
```

The runner builds two binaries from exactly the same source, compiler and common flags (`-O3 -g -std=c++20 -pthread`). The baseline has no probes and no otelc runtime. The instrumented binary uses selective LLVM probes and the runtime. It runs three lanes in alternating order:

| Lane | Behaviour |
| --- | --- |
| `baseline` | Plain build, no probes/runtime |
| `probes_disabled` | Instrumented build, without `OTELC_CONFIG`; telemetry remains disabled |
| `metrics_enabled` | Instrumented build, admitted timing, worker/exporter and final flush |

All lanes must produce the same application checksum. Startup-sensitive environment settings are removed so a shell's `OTEL_*` or `OTELC_*` settings do not alter the comparison. A thousand warm-up calls run before measurement; they still count in the telemetry correctness check. Worker threads are created before the measured interval and synchronise at its start.

## Results and interpretation

Results are written to `build/benchmarks/results.md` and `results.json`. The JSON retains every sample, checksum, compiler identity, common flags, source digest, binary sizes, host/architecture, repetitions, workload bounds and per-run observation reports.

The report compares workload median, minimum, maximum and standard deviation, percentage change, estimated additional nanoseconds per timed call, and whole-process median. Workload time includes the loop, scheduling and thread joins, while excluding runtime/process initialisation and final export. Whole-process time includes startup, warm-up, final flush and report writing. A single-thread run is usually the clearest probe-cost comparison; multi-thread results include scheduling and queue-drain behaviour.

`complete_telemetry` is true only if every active run drained, completed its exporter, reported no lost observations or dropped export batches, and counted the known calls. The runner retains losses rather than hiding or reclassifying them. HTTP acceptance and an observation report do not prove that the downstream database retained the batch; the Docker stack's Prometheus/Grafana verification is separate evidence.

A false telemetry-completeness result means the measured workload speed must be read alongside lost observations. Increasing workload intensity can saturate the fixed queues. Comparing a baseline with an instrumented run that drops much of its telemetry is not an equivalent-output performance result.

This is a repeatable internal workload comparison, not a universal overhead guarantee or a release performance qualification. Run on an otherwise quiet machine, repeat the comparison, retain the raw output and compare identical settings. A workload-median delta is not a per-invocation latency distribution; p50/p95/p99 probe latency, allocations, CPU time and representative application throughput remain release-qualification work.

## Local measurement: 6 October 2026

On this macOS ARM64 host with Homebrew Clang 22.1.8 and the prototype runtime, eight repetitions produced the following workload medians. These are dated observations, not release targets.

| Workload | Plain | Probes disabled | Metrics enabled | Extra active ns/call |
| --- | ---: | ---: | ---: | ---: |
| 1 thread, 100,000 calls | 1.379 ms | 1.951 ms | 13.890 ms | 125.1 |
| 4 threads, 50,000 calls each | 1.376 ms | 1.609 ms | 34.099 ms | 163.6 |

Checksums matched and all active runs reported complete telemetry. Whole-process medians with telemetry were 268.988 ms and 297.841 ms respectively; shutdown/export costs matter for short processes. The local raw reports are in `build/benchmarks/single-thread/` and `build/benchmarks/four-threads/`. They are generated evidence, excluded from Git; regenerate them when the compiler, runtime, workload or host changes.

## Observation reports

An explicit `OTELC_REPORT_PATH` asks the runtime to write a small JSON summary at normal shutdown. It records processed function calls/object lifetimes, admission/stack/queue/invalid/incomplete losses, export completion and dropped batches. No report I/O occurs inside a function probe. Missing reports, failed process exits or differing checksums fail the benchmark rather than manufacturing results.

Benchmarks do not change the instrumentation configuration of other applications, and they do not delete Prometheus/Grafana data.

## Live metrics latency in one process

`make benchmark-live LIVE_BENCHMARK_ARGS='--iterations 50000 --runs 8'` compares a plain process with metrics-off/on phases of the same instrumented process. It uses the unchanged `examples/apps/live-latency.cpp`, identical compiler/flags, warm-up and alternating on/off phase order. Body timing excludes control requests, startup and shutdown. The report at `build/benchmarks/live/report.json` retains every sample, PID, source hash, compiler/flags, checksum and shutdown/loss evidence. It reports added metrics latency separately from the remaining disabled-probe overhead and total instrumentation overhead. See the [developer 101](developer-101.md#measure-the-added-latency).

The live native and language benchmarks remove the previous `runtime.json` and `report.json` before each run. A failed run cannot reuse those previous results. Language benchmarks also require the shutdown report PID to match the control socket's application PID, which can differ from a build/launch wrapper's PID. Private control sockets use short temporary paths under `/tmp`, so long result-directory paths do not exceed the Unix socket path limit. Use a separate output directory for each concurrent run and for results you want to preserve.

All Rust language benchmarks retain the instrumented executable, build manifest, compiler evidence and raw build channels in a unique `rust-build-*/bundle` directory under the chosen output directory. The plain comparison uses the exact compiler and `baseline_flags` from that manifest. `report.json` records the directory and metadata under `rust_build`; replay validates the bundle's fingerprints before launching the app. The default stable compiler profile supports the synchronous, async and closure benchmark fixtures:

```sh
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS='--iterations 10000 --runs 8'
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS='--rust-async --iterations 10000 --runs 8 --output build/benchmarks/rust-async'
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS='--rust-closures --iterations 10000 --runs 8 --output build/benchmarks/rust-closures'
```

For the [experimental Rust LLVM backend](rust-compiler.md), first build its pinned profile, then run:

```sh
make benchmark-language LANGUAGE=rust LANGUAGE_BENCHMARK_ARGS='--rust-llvm --iterations 10000 --runs 8'
```

Both LLVM lanes disable the same MIR inlining passes. Every Rust mode replays one instrumented process through metrics-off/on phases without rebuilding or restarting. Reports retain the source hash, control PID, every sample and exact call/loss checks. `--rust-async` is rejected for the LLVM profile. These workload timings do not qualify production overhead, and metrics-off still includes compiled probes and SDK startup.
