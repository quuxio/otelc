# Compare application latency and instrumentation cost

<!-- markdownlint-disable MD013 -->

Java and the JavaScript/Microsoft TypeScript and Python candidates use the same `integer128-v1` workload and one common policy. Every accepted batch must match the independent checksum. Plain and instrumented application sources remain unchanged. Five measured modes are retained: `plain`, `both_off`, `collection_only`, `emission_only` and `both_on`. All four instrumented modes use the same application PID and build. Tracing and lifetime observation retain their configured settings while metrics change live.

## Run equivalent language comparisons

Prepare the tools and local stack:

```sh
make setup java-build node-observer node-http
make stack-up
```

Use a fresh comparison ID to group the language reports in Grafana. Apply the same policy to each language. Java remains the default for `make benchmark` and the CLI. Use `make benchmark BENCHMARK_LANGUAGE=python` for a standalone Python run.

```sh
comparison=$(date +%Y%m%d-%H%M%S)
bin/quux-otelc --config examples/otelc.toml benchmark \
  --language java --comparison-id "$comparison" \
  --output "build/benchmarks/$comparison-java"
bin/quux-otelc --config examples/otelc.toml benchmark \
  --language javascript --comparison-id "$comparison" \
  --output "build/benchmarks/$comparison-javascript"
bin/quux-otelc --config examples/otelc.toml benchmark \
  --language typescript --backend native --comparison-id "$comparison" \
  --output "build/benchmarks/$comparison-typescript"
bin/quux-otelc --config examples/otelc.toml benchmark \
  --language python --comparison-id "$comparison" \
  --output "build/benchmarks/$comparison-python"
```

The default TypeScript backend is Microsoft's pinned native compiler. Use `--backend api` for the pinned Microsoft compiler API profile. Compare the two compiler profiles with separate comparison IDs, so the dashboard does not combine their TypeScript samples. Compilation/startup occurs before warmup and is excluded from operation latency. A TypeScript plain lane uses the selected real compiler without probes or an SDK process; JavaScript plain uses Node directly. Python uses the public prepared native launcher in both lanes. Its plain lane retains the native entry journal and lifecycle checks, with ordinary application startup but no generated instrumentation hook or SDK. Preparation and source snapshots occur outside the application-body timer. No production GC request is added.

Set `[benchmark]` `iterations`, `warmup` and `samples` in a copy of the common policy to change the measurement size. Use the same copy for all compared languages. Use `--no-publish` to retain local reports without sending driver measurements to the Collector. That flag does not change the application's export policy.

## Read the retained evidence

`report.json` contains raw ns/operation arrays, medians, comparison/run identity, source and policy hashes, runtime/compiler/build identity, host information and state transitions. `responses.jsonl` retains each parsed Node/Python application response before SDK flush or resource sampling can fail. `raw.jsonl` retains complete samples, including the driver-observed PID, checksum and resource windows. A failed later observation never becomes a successful CPU sample. Warmup and state/control/pipe time are excluded from the application's batch timer. Java shuffles instrumented states with a reproducible per-sample seed. Node and Python rotate the four states so that each occupies every position over four samples, then reverses the next four-sample block; the report records this method. Neither schedule removes all host load or time-order bias. Use enough samples and inspect the raw sequence.

Node reports also retain `plain/` and `instrumented/` launch/compiler/input evidence, independent SDK drain reports and stderr. The driver refuses publication on input mutation, incomplete final accounting, handoff/observer/queue loss, failed exports or a failed application. Raw files remain available after failure. A clean SDK report establishes producer accounting; it does not prove downstream receipt or OTLP partial-rejection absence.

Python reports `launch_profile=prepared-native-v1` and retains the public original request, resolved recipe, native tool/build manifest, consumed entry journal and actual supervisor/application/SDK completion records in `plain/` and `instrumented/`. `pids` identifies the applications; `supervisor_pids` identifies their separate owners. `launch_evidence` binds the retained files, including the instrumented SDK drain report, and is rechecked after both lanes complete. Changed inputs/evidence, unknown accounting, process failures, incomplete journals and lost/failed/pending exports prevent the final report and driver publication. Raw samples remain after failure. The consumed entry is checked; full import/code/dependency provenance remains unqualified and `source_complete` stays false. Use separate comparison IDs for older lower-level launch results.

Node and Python `process_samples` retain separate application and SDK CPU/RSS observations made with the operating system's `ps` command. Both Python lanes also retain supervisor observations. CPU deltas cover the batch and explicit SDK flush, including intervening process/control activity. They are not the application-body latency timer. RSS is the maximum of before/after snapshots, not peak allocation or total process peak. CPU precision is limited by the platform's `ps` display; a zero delta can mean below resolution. Small functional runs establish correctness, not reliable performance estimates. Sampling and flushing can also affect scheduling and later batches. Java does not yet provide these per-process resource windows.

Lifetime and trace configuration remain in every raw report. To measure lifecycle-observer cost, repeat the complete comparison under a separately labelled common policy with `[lifetimes] enabled = false`, keeping the remaining settings fixed. Do not interpret metric-off states as removing installed probes, trace work or lifecycle bookkeeping. Repeat runs on an otherwise idle host and inspect variation before drawing performance conclusions.

## View the comparison in Grafana

Open [language performance comparison](http://localhost:3001/d/otelc-comparison) and select the comparison ID. The dashboard shows mean ns/operation, overhead relative to each language's plain baseline, throughput, sample counts and toolchain identity. Separate Node/Python panels show CPU/operation and RSS by role, including the Python supervisor. The dashboard uses exported driver measurements rather than sampled function-duration histograms.

Use [function metrics and lifetimes](http://localhost:3001/d/otelc-lifetimes) for application telemetry and [task/service traces](http://localhost:3001/d/otelc-traces) for trace trees. The local stack uses OTLP/HTTP port 4320, Prometheus 9091, Tempo 3201 and Grafana 3001. Export success alone is insufficient: check the Collector, Prometheus samples and, when traces are enabled, decoded Tempo spans. Missing language series mean that no qualified results were published for that selection; they are never filled with synthetic values.

## View completed runs

Select a dashboard time range that includes the run, then choose its comparison ID. The panels use the latest retained sample for each series within that range, so completed runs remain visible after the Collector expires their live series. The run selector refreshes when the time range changes. Runtime identity rows omit the query evaluation time; it is not the benchmark recording time.

The local Collector expires inactive metric series after one hour, while Prometheus retains scraped history for seven days. For older results, retain the raw report and sample files. Stopping the stack preserves its named data volumes. Historical throughput and overhead still describe the recorded workload; they are not current application rates.
