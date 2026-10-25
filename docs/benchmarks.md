# Compare application latency and instrumentation cost

<!-- markdownlint-disable MD013 -->

Java and the JavaScript/Microsoft TypeScript, Python and [C/C++ candidates](native-benchmarks.md) use the same `integer128-v1` workload and one common policy. Every accepted batch must match the independent checksum. Plain and instrumented application sources remain unchanged. Five measured modes are retained: `plain`, `both_off`, `collection_only`, `emission_only` and `both_on`. All four instrumented modes use the same application PID and build. Tracing and lifetime observation retain their configured settings while metrics change live.

## Run equivalent language comparisons

Prepare the tools and local stack:

```sh
make setup java-build node-observer node-http
make stack-up
```

Use a fresh comparison ID to group the language reports in Grafana. Apply the same policy to each language. Java remains the default for `make benchmark` and the CLI. Use `make benchmark BENCHMARK_LANGUAGE=python` for a standalone Python run. The [native guide](native-benchmarks.md) adds the matching SDK prerequisite, shared metrics-only policy and C/C++ commands. The separate [C lifetime candidate](c-resources.md) supports ordinary record-pointer allocations and `free` through the shared example policy; broader lifetime boundaries remain work.

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

The default TypeScript backend is Microsoft's pinned native compiler. Use `--backend api` for the pinned Microsoft compiler API profile. Compare the two compiler profiles with separate comparison IDs, so the dashboard does not combine their TypeScript samples. Compilation/startup occurs before warmup and is excluded from operation latency. A TypeScript plain lane uses the selected real compiler without probes or an SDK process; JavaScript plain uses Node directly. Python's plain lane now uses CPython directly, with the same ordinary source and interpreter environment. It has no otelc native input observer, generated bootstrap, supervisor or SDK process. The instrumented lane keeps the public prepared native launcher and its independent SDK. Preparation, launch and ordinary startup imports occur before application-body timing; imports triggered inside the measured workload remain timed. No production GC request is added.

Set `[benchmark]` `iterations`, `warmup` and `samples` in a copy of the common policy to change the measurement size. Use the same copy for all compared languages. Use `--no-publish` to retain local reports without sending driver measurements to the Collector. That flag does not change the application's export policy.

## Read the retained evidence

Java, Node, Python and native C/C++ benchmark drivers keep the first measurement, cancellation or cleanup error as the primary failure. Shutdown, final SDK checks and input rechecks still run; later failures are retained as notes, including in CLI diagnostics. A cleanup failure also fails an otherwise successful run. Available raw samples and final records remain diagnostic evidence. Measurement and final application/SDK-validation failures prevent the final report and driver export. Closing the driver exporter can still fail after the Collector has accepted its metrics; that failure is reported, without claiming to retract the accepted data.

`report.json` contains raw ns/operation arrays, medians, comparison/run identity, source and policy hashes, runtime/compiler/build identity, host information and state transitions. `responses.jsonl` retains each parsed Java/Node/Python application response before SDK flush or resource sampling can fail. `raw.jsonl` retains complete samples, including the driver-observed PID, checksum and resource windows. A failed later observation never becomes a successful CPU sample. Warmup and state/control/pipe time are excluded from the application's batch timer. Java, Node and Python rotate the four states so that each occupies every position over four samples, then reverse the next four-sample block; the report records this method. Neither schedule removes all host load or time-order bias. Use enough samples and inspect the raw sequence.

Node reports also retain `plain/` and `instrumented/` launch/compiler/input evidence, independent SDK drain reports and stderr. The driver refuses publication on input mutation, incomplete final accounting, handoff/observer/queue loss, failed exports or a failed application. Raw files remain available after failure. A clean SDK report establishes producer accounting; it does not prove downstream receipt or OTLP partial-rejection absence.

Python reports `launch_profile=stock-plain-prepared-codes-v5`. The plain directory retains its stock request, actual direct-process start/exit and first-response record. Its original source/config/interpreter/tooling file identities are checked before and after execution and again after the instrumented lane. There is no native journal and no consumed-source provenance claim for stock plain. `supervisor_pids.plain` and its SDK identity are explicitly null; CPU/RSS samples contain only the actual plain application.

The instrumented directory retains the public request, resolved recipe, native tool/build manifest, consumed entry/ordinary-loader journal, code-v2 descriptors, actual supervisor/application/SDK completion and checked SDK drain. Completion evidence is read after owned shutdown and both pipe-close attempts, even when measurement code raises. Available SDK drain evidence is then validated. Successful shutdown and validation preserve the original body exception; cleanup, completion and drain errors retain it in their exception chain or cleanup notes. If failed completion leaves no SDK report, the error states that drain validation was unavailable; a missing report on otherwise normal exit is refused. A failed launch that never yields an application has no available SDK report to validate. `pids` identifies the applications and `supervisor_pids.instrumented` its separate owner. `launch_evidence` binds all retained evidence, including first-response records, and is rechecked after both lanes complete. Changed inputs/evidence, unknown accounting, process failures, incomplete instrumented journals and lost/failed/pending exports prevent the final report and driver publication. Raw samples remain after failure. Source/pyc attribution, whole-program code and dependency provenance remain unqualified; `source_complete` stays false.

`launch_to_first_response` separately records one observation per lane: immediately before direct stock application creation or prepared supervisor creation through the first successful one-call workload response. It includes verification/readiness, SDK startup where present, metric-off control, pipe and first-body time. Preparation/build work is outside that interval. This is not pure interpreter startup, and one observation is not a performance distribution. The extra one-call handshake is outside warmup and measured batches; it does not add a measured sample. Tracing/lifetimes retain their configured behaviour, while metrics are off during the handshake and warmup.

Use separate comparison IDs for historical prepared plain profiles: `prepared-native-v1`, `prepared-native-imports-v2`, `prepared-native-codes-v3` and `prepared-native-codes-v4`. They included native input-observer startup/RSS costs in plain and remain useful for their recorded evidence comparisons. Their plain scope must not be relabelled as stock.

Java, Node and Python `process_samples` retain CPU/RSS observations made with the operating system's `ps` command. Java reports the actual JVM as `application`: the instrumented JVM includes its embedded agent and SDK, with no separate SDK process or invented zero-cost SDK sample. Node and Python retain separate application and SDK observations. The instrumented Python lane also retains supervisor observations; the stock plain lane has only the application process. CPU deltas cover the batch and explicit SDK flush, including intervening process/control activity. They are not the application-body latency timer. RSS is the maximum of before/after snapshots, not peak allocation or total process peak. CPU precision is limited by the platform's `ps` display; a zero delta can mean below resolution. Small functional runs establish correctness, not reliable performance estimates. Sampling and flushing can also affect scheduling and later batches. Java uses the same schema-2 report and resource windows. Earlier Java schema-1 reports contain body latency only; missing resource observations are not zero measurements.

After Node/Python sampling, the driver restores collection and emission in the same application/SDK processes and explicitly flushes the last retained collection-only values. This step is outside the latency and CPU sample windows. `metric-finalization.json` retains both control responses, and its file identity is included in the report. A failed flush or changed process/state prevents publication; raw samples remain available.

Lifetime and trace configuration remain in every raw report. To measure lifecycle-observer cost, repeat the complete comparison under a separately labelled common policy with `[lifetimes] enabled = false`, keeping the remaining settings fixed. Do not interpret metric-off states as removing installed probes, trace work or lifecycle bookkeeping. Repeat runs on an otherwise idle host and inspect variation before drawing performance conclusions.

The [Rust comparison candidate](rust-benchmarks.md) uses the same schema-2 results and Grafana queries with `language=rust`. Its SDK is embedded and counted once in application CPU/RSS. It requires the retained original example build and an external new output directory. Rust lifetime and task/service profiles remain separate unfinished work.

## View the comparison in Grafana

Open [language performance comparison](http://localhost:3001/d/otelc-comparison) and select the comparison ID. The dashboard shows mean ns/operation, overhead relative to each language's plain baseline, throughput, sample counts and toolchain identity. Separate resource panels show Java/Node/Python/C/C++ CPU/operation and RSS by role, including Python and native instrumented supervisors. Native plain is a direct executable and the C++ SDK is embedded in the instrumented application. Compare Java's instrumented JVM with the other languages' application and SDK roles together when assessing total instrumentation cost. The dashboard uses exported driver measurements rather than sampled function-duration histograms.

Use [function metrics and lifetimes](http://localhost:3001/d/otelc-lifetimes) for application telemetry and [task/service traces](http://localhost:3001/d/otelc-traces) for trace trees. The local stack uses OTLP/HTTP port 4320, Prometheus 9091, Tempo 3201 and Grafana 3001. Export success alone is insufficient: check the Collector, Prometheus samples and, when traces are enabled, decoded Tempo spans. Missing language series mean that no qualified results were published for that selection; they are never filled with synthetic values.

## View completed runs

Select a dashboard time range that includes the run, then choose its comparison ID. The panels use the latest retained sample for each series within that range, so completed runs remain visible after the Collector expires their live series. The run selector refreshes when the time range changes. Runtime identity rows omit the query evaluation time; it is not the benchmark recording time.

The local Collector expires inactive metric series after one hour, while Prometheus retains scraped history for seven days. For older results, retain the raw report and sample files. Stopping the stack preserves its named data volumes. Historical throughput and overhead still describe the recorded workload; they are not current application rates.

The Python launch-to-first-response panel shows the separately scoped launch observation for each lane. Its values include readiness, controls and IPC; compare them separately from body latency. RSS is a snapshot of each real process, not peak memory or allocated bytes. The plain Python lane has no synthetic zero-cost supervisor or SDK series.
