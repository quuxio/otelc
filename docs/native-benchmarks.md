# C and C++ performance comparison candidate

<!-- markdownlint-disable MD013 -->

The native candidate adds ordinary [C](../examples/c/BenchmarkApp.c) and [C++](../examples/cpp/BenchmarkApp.cpp) versions of the same `integer128-v1` workload used by Java, JavaScript, Microsoft TypeScript and Python. The common schema-3 policy drives instrumentation, tracing, lifetimes and live metric controls. Applications contain no OpenTelemetry imports, annotations, probes or bootstrap. This is a benchmark candidate; independent review, protected delivery and oteleq qualification remain separate requirements.

## Run with one common policy

Use the [shared metrics comparison policy](../examples/benchmark-metrics.toml) for every language in a comparison. It disables lifetime observation consistently. To include the declared lifetime observers, use the unchanged [example policy](../examples/otelc.toml) for every language instead: C observes allocated `WorkItem` records until `free` entry, while C++ observes construction until destructor entry. The C workload deliberately retains its allocation with a volatile pointer; original source hashes and stock timings identify this workload version. These different lifetime boundaries must not be presented as identical durations.

With the documented [native prerequisites](native-launch.md), build the matching SDK and start the [local collection stack](benchmarks.md#run-equivalent-language-comparisons):

```sh
make native-launch-sdk
make stack-up
comparison=$(date +%Y%m%d-%H%M%S)
bin/quux-otelc --config examples/benchmark-metrics.toml benchmark \
  --language c --sdk-build build/native-launch-sdk \
  --comparison-id "$comparison" --output "build/comparison/$comparison/c"
bin/quux-otelc --config examples/benchmark-metrics.toml benchmark \
  --language cpp --sdk-build build/native-launch-sdk \
  --comparison-id "$comparison" --output "build/comparison/$comparison/cpp"
```

Use new output directories for every run. `--no-publish` retains local reports without publishing driver measurements; the application still follows its configured telemetry endpoint. Reuse the same comparison ID and unchanged configuration with `--language java`, `javascript`, `typescript` and `python` to group those delivered benchmark paths in Grafana. Prepare their tools as described in the [shared benchmark guide](benchmarks.md). Go and Rust benchmark adapters remain work; a dashboard language label does not imply an implemented adapter.

The Make target also accepts the common policy:

```sh
make benchmark BENCHMARK_LANGUAGE=cpp BENCHMARK_CONFIG=examples/benchmark-metrics.toml
```

For the additional cost of C++ lifetime observation, repeat the C++ comparison with `examples/otelc.toml` and a different comparison ID. It selects the ordinary `WorkItem` class through the common lifetime pattern. Constructor return starts an observation and destructor entry ends it. A successfully completed benchmark requires every admitted item to complete; censored or lost items prevent publication. Keep trace, sampling, workload and compiler settings fixed when comparing the two policies. Metric-off modes retain configured trace and lifetime work.

## What the comparison measures

Each batch performs the same 128-step integer recurrence for every seed and returns its checksum, call count and elapsed nanoseconds. The driver independently computes the expected checksum. The C timer uses `CLOCK_MONOTONIC_RAW` on the supported macOS/Linux profile; C++ uses `steady_clock`. The timer surrounds the application batch body, excluding response formatting, control calls, resource sampling and explicit SDK flushes.

The driver copies the maintained example bytes unchanged into a private source tree, preserving `examples/...` policy paths. It records and rechecks both original source identity and compiled inputs/products. The direct plain executable contains no SDK, bootstrap, pre-exec gate or supervisor. The instrumented lane uses the public owned native preparation/release contract, an embedded C++ SDK and a separate supervisor. A matching SDK must be built from the current checkout; another checkout's rewritten build receipt is not a substitute.

One plain process supplies baseline batches. One instrumented application executes `both_off`, `collection_only`, `emission_only` and `both_on` without a rebuild or restart. The common rotating/reversing schedule reduces state-position bias. Every accepted sample matches the checksum and actual application PID. Emission-off must reach the SDK's real export barrier before timing. After sampling, the same process restores both switches and flushes retained collection-only measurements outside the measured windows.

CPU/RSS samples identify the real application process. The instrumented application includes its embedded SDK; its supervisor has a separate sample. There is no invented SDK process or synthetic zero-cost plain supervisor. CPU deltas cover the batch and explicit SDK flush/control window. RSS is the maximum of before/after snapshots, rather than peak allocation. Platform `ps` resolution can produce a zero CPU delta for a short batch. The common report keeps those resource windows separate from application-body latency.

One launch-to-first-response observation per lane includes actual process creation, ownership/readiness, metric-off control, IPC and one successful workload call. Build and preparation are outside that interval. It is neither pure runtime startup nor a latency distribution.

## Retained evidence and Grafana

`report.json`, `raw.jsonl` and `responses.jsonl` use the shared schema-2 format. The report retains original source, compiler/build policy, both application PIDs, the instrumented supervisor, per-role samples, final metric restoration and launch/completion/health bindings. Each lane retains stderr and first-response data. The instrumented lane also retains public request/plan/preparation, actual pre-exec ownership/release, execution and final SDK health. Both lanes' evidence and compiled inputs are rechecked after completion.

Unknown accounting, active/lost/invalid function frames, incomplete lifetime intervals, queued/dropped spans, failed or pending SDK exports, failed flush/shutdown, a changed input, a bad checksum or a failed application prevents a final successful report and driver publication. Raw samples and available final SDK evidence remain for diagnosis. A body failure still attempts owned shutdown and final drain validation. SDK accounting does not establish downstream storage, OTLP partial-rejection absence, complete compiler/source origin or installed-library closure; `source_complete=false` remains explicit.

Open [language performance comparison](http://localhost:3001/d/otelc-comparison) and select the comparison ID and a time range containing the run. Its existing language/state panels show body latency, overhead against each language's plain baseline, throughput, CPU/RSS roles, sample counts and compiler identity. The [lifetime dashboard](http://localhost:3001/d/otelc-lifetimes) and [Tempo trace dashboard](http://localhost:3001/d/otelc-traces) show configured application telemetry. Verify Collector acceptance and actual Prometheus/Tempo data separately from the SDK checkpoint. Missing language series are not zero-cost results.

Small test runs establish functional comparison and export behaviour. Use sufficient iterations/samples on an otherwise idle host before making performance claims. Toolchain optimisation, trace sampling and object construction/destruction differ between languages; matching work and configuration does not remove those differences.

## Contributor check

```sh
make native-benchmark-check
```

The integration setup requires at least 8 GiB free and refuses before SDK setup when space is insufficient. This builds a genuine matching SDK unless `OTELC_BENCHMARK_SDK` selects an existing one. Tests run actual plain and instrumented C/C++ applications, check all four live states and retained metrics, decode OTLP, verify original source, observe real process roles and reject checksum/loss/export failures. Each new driver module has an independent 80% coverage floor. The full gate, independent review, protected merge and actual downstream demonstration remain required before delivery.

The focused `Native benchmark diagnostics` workflow runs the unchanged stock/instrumented corpus on Linux and macOS. It preserves original HTTP entity bytes before gzip/protobuf decoding, final health, private SDK logs, immediate health after a failed flush and the tested images. Capture is limited to 1,024 requests and 64 MiB of entity bodies per receiver; exhausting that budget fails the capture. Request headers and control descriptors/plans are omitted. These records describe received request entities, not response acknowledgements or independent downstream storage. Required Linux/macOS jobs also retain this compact diagnostic allowlist. A failed flush still fails the original test; no retry, timeout or export-acceptance rule changes.
