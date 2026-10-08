# Developer 101: instrument unchanged code and produce metrics

## What you can run today

This guide uses the locally tested macOS ARM64 C/C++ LLVM backend. It instruments application functions without editing original files, reads optional existing Clang function annotations, preserves C++ exceptions and can turn metrics admission on/off in a running process. Rust, TypeScript/JavaScript, Java, Python and Go have implemented function-metrics adapters using the same configuration contract. Their language-specific build, source and annotation examples are in the [language guides](languages.md). Automatic object lifetimes and trace export remain TODO.

The compiler wrapper adds probes in LLVM IR and links the native runtime. The runtime aggregates completed function counts and inclusive duration histograms, then sends OTLP/HTTP protobuf to the Collector. Prometheus stores the scraped data; Grafana displays it. Application arguments and return values are not exported.

## Prepare the tools and metrics viewer

Run from the repository root. On this Mac the checkout is `/Users/sclarke/github/otelc`. The qualified local requirements are Rust 1.98.1, Homebrew LLVM 22, a Docker engine and Docker Compose.

```sh
cd /Users/sclarke/github/otelc
brew install llvm@22
make setup
make build
colima start
make stack-up
mkdir -p build/tutorial
```

Open [Grafana](http://localhost:3000/d/otelc-local) and [Prometheus](http://localhost:9090). The stack provisions its datasource/dashboard and permits anonymous read-only viewing. See [stack operation and troubleshooting](observability-stack.md) for persistence, ports and Docker details. `doctor` checks compiler/runtime/configuration availability; it does not prove Collector connectivity.

## Example 1: no annotations or instrumentation calls

The complete source in [`examples/apps/config-only.c`](../examples/apps/config-only.c) is:

```c
#include <stdio.h>

int process_order(int value) { return value * 2; }
int audit_order(int value) { return value + 1; }

int main(void) {
    int result = 0;
    for (int i = 0; i < 4; ++i)
        result += process_order(i) + audit_order(i);
    printf("result=%d\n", result);
    return 0;
}
```

No otelc include, annotation, guard or runtime call is required. The external configuration in [`examples/config-only.toml`](../examples/config-only.toml) is:

```toml
schema_version = 2
languages = ["c", "cpp"]

[sources]
include = ["examples/apps/**"]
exclude = []

[functions]
include = ["process_order*"]
exclude = []

[annotations]
read_existing = false
inject_generated = false

[export]
endpoint = "http://127.0.0.1:4318"
interval_ms = 1000

[resource]
service_name = "otelc-config-only-example"

[adapters.c]
backend = "llvm"
[adapters.cpp]
backend = "llvm"
```

Source patterns select the original translation unit. Function patterns select qualified names; only `*` and `?` are name wildcards. Exclusions win. Empty includes supply no configuration opt-ins. With annotation reading disabled, that selects no functions.

Build and run:

```sh
./target/debug/quux-otelc --config examples/config-only.toml --language c config --require-supported
./target/debug/quux-otelc --config examples/config-only.toml --language c doctor
./target/debug/quux-otelc --config examples/config-only.toml clang -O2 -g \
  examples/apps/config-only.c -o build/tutorial/config-only
./target/debug/quux-otelc inspect build/tutorial/config-only
./target/debug/quux-otelc --config examples/config-only.toml --language c run ./build/tutorial/config-only
```

The app prints `result=22`. A fresh process contributes four `process_order` calls and no `audit_order` calls. The original source stays byte-for-byte identical. The compiler infers C/C++ during compilation; schema-2 `run` needs `--language`. Build and run with the same file. The executable's adjacent `.otelc.json` manifest must be retained; it binds selection to the binary identity.

For a matching uninstrumented build:

```sh
/opt/homebrew/opt/llvm@22/bin/clang -O2 -g examples/apps/config-only.c -o build/tutorial/config-only-plain
./build/tutorial/config-only-plain
```

It prints the same result and produces no otelc metrics.

## Example 2: the source already contains annotations

Annotation reading is optional. When a codebase already has supported metadata, `annotations.read_existing = true` reads it through Clang's semantic LLVM metadata. It does not rewrite source or interpret comments with regex. Without annotations, external function rules continue to work.

The complete [`examples/apps/annotated.c`](../examples/apps/annotated.c) is:

```c
#include <stdio.h>

__attribute__((annotate("otelc.instrument")))
int process_order(int value) { return value * 2; }

__attribute__((annotate("otelc.exclude")))
int audit_order(int value) { return value + 1; }

__attribute__((annotate("otelc.instrument")))
int blocked_order(int value) { return value + 2; }

__attribute__((annotate("vendor.audit")))
int vendor_order(int value) { return value + 3; }

__attribute__((annotate("otelc.instrument"), annotate("otelc.exclude")))
int conflicted_order(int value) { return value + 4; }

int configured_order(int value) { return value + 5; }

int main(void) {
    int result = 0;
    for (int i = 0; i < 4; ++i)
        result += process_order(i) + audit_order(i) + blocked_order(i)
            + vendor_order(i) + conflicted_order(i) + configured_order(i);
    printf("result=%d\n", result);
    return 0;
}
```

[`examples/annotated.toml`](../examples/annotated.toml) uses the same schema and Collector settings as the first example, with these selection settings and service name `otelc-annotated-example`:

```toml
[functions]
include = ["configured_order*", "audit_order*", "conflicted_order*"]
exclude = ["blocked_order*"]

[annotations]
read_existing = true
inject_generated = false
```

| Function | Why it is selected or excluded | Calls per C process |
| --- | --- | --- |
| `process_order` | `otelc.instrument` opts in without a matching include rule | 4 |
| `configured_order` | External include rule, no annotation required | 4 |
| `audit_order` | `otelc.exclude` overrides the include rule | 0 |
| `blocked_order` | External exclusion overrides `otelc.instrument` | 0 |
| `conflicted_order` | Annotation opt-out wins over annotation opt-in | 0 |
| `vendor_order` | Unrelated annotation is preserved and does not opt in | 0 |

Both annotations apply to functions only. Unknown `otelc.*` function annotations are errors when reading is enabled. Unrelated annotation namespaces are preserved. Runtime functions remain protected. If `read_existing` is false, all annotations are ignored for selection, including opt-outs; the external include/exclude rules still apply. Generated annotation injection remains unsupported and must stay false.

```sh
./target/debug/quux-otelc --config examples/annotated.toml --language c config --require-supported
./target/debug/quux-otelc --config examples/annotated.toml clang -O2 -g \
  examples/apps/annotated.c -o build/tutorial/annotated-c
./target/debug/quux-otelc inspect build/tutorial/annotated-c
./target/debug/quux-otelc --config examples/annotated.toml --language c run ./build/tutorial/annotated-c
```

The C app prints `result=102`. `inspect` reports `included by annotation` for compiled annotation opt-ins. Reading annotations requires the LLVM backend; callbacks reject that configuration.

The [C++ version](../examples/apps/annotated.cpp) includes the same cases plus an annotated throwing function:

```cpp
__attribute__((annotate("otelc.instrument")))
int throw_order(int value) {
    if (value < 0) throw std::runtime_error("invalid order");
    return value;
}
```

It is called with `-1` and caught by the existing application logic. Build with exceptions enabled:

```sh
./target/debug/quux-otelc --config examples/annotated.toml clang++ -O2 -g -std=c++20 \
  examples/apps/annotated.cpp -o build/tutorial/annotated-cpp
./target/debug/quux-otelc inspect build/tutorial/annotated-cpp
./target/debug/quux-otelc --config examples/annotated.toml --language cpp run ./build/tutorial/annotated-cpp
```

It prints `result=109`, contributes four calls each for `process_order(int)` and `configured_order(int)`, and one `throw_order(int)` call with one exceptional exit. No `-fno-exceptions` is added. The tests compare both annotated apps with plain builds and verify source bytes, exact telemetry, disabled annotation reading, conflicting annotations and external exclusion priority.

## Find the produced metrics

Allow a few seconds for Collector batching, the one-second scrape and Grafana refresh. In Prometheus, query:

```promql
sum by (code_function_name) (otelc_function_calls_total{service_name="otelc-config-only-example"})
sum by (code_function_name) (otelc_function_calls_total{service_name="otelc-annotated-example"})
sum(otelc_function_unwinds_total{service_name="otelc-annotated-example"})
sum(otelc_runtime_dropped_observations_total{service_name="otelc-annotated-example"})
sum(otelc_export_dropped_batches_total{service_name="otelc-annotated-example"})
```

Repeated runs add distinct process-instance series, so sums can exceed the per-process counts above. Query `service_instance_id` to isolate one run. Duration histograms measure inclusive time in the selected functions. Grafana's p95 estimate describes instrumented execution; it does not by itself measure instrumentation overhead. Use the paired benchmark below for added latency.

## Turn metrics off/on without restarting

The unchanged [live example source](../examples/apps/live-latency.cpp) accepts `batch N`, `hold` and `quit` on standard input. It contains no otelc API or annotations. Its hot function is:

```cpp
std::uint64_t process_order(std::uint64_t value) {
    for (int j = 0; j < 16; ++j) {
        value ^= value >> 30; value *= 0xbf58476d1ce4e5b9ULL;
        value ^= value >> 27; value *= 0x94d049bb133111ebULL;
        value ^= value >> 31;
    }
    return value;
}
```

[`examples/live.toml`](../examples/live.toml) selects that function and `held_order`, uses LLVM, starts with `metrics.enabled = false`, and opts into a control socket:

```toml
[runtime]
control_socket = "build/control/metrics.sock"

[metrics]
enabled = false
```

The parent directory must already be owned by your user with mode 0700. The socket has mode 0600. An occupied path is rejected rather than removed. Use a separate path for each concurrent app. The endpoint is created only when the runtime successfully initialises.

Terminal 1, from the repository root:

```sh
mkdir -p build/control
chmod 700 build/control
./target/debug/quux-otelc --config examples/live.toml clang++ -O2 -g -std=c++20 \
  examples/apps/live-latency.cpp -o build/tutorial/live
./target/debug/quux-otelc --config examples/live.toml --language cpp run ./build/tutorial/live
```

After `ready`, enter `batch 50000` as application input. It prints body elapsed nanoseconds, checksum and call count. Terminal 2, from the same root:

```sh
./target/debug/quux-otelc status --socket build/control/metrics.sock
./target/debug/quux-otelc enable --socket build/control/metrics.sock
./target/debug/quux-otelc status --socket build/control/metrics.sock
./target/debug/quux-otelc disable --socket build/control/metrics.sock
```

Run another `batch 50000` in Terminal 1 after each toggle. Status returns the same PID, enablement and worker-observed completed function count. No rebuild, restart or source change is involved. Enter `quit` to exit; normal shutdown removes the socket.

Off stops admission of new measurements. Already admitted calls retain their token and complete normally, even if they throw while off. Calls started while off remain unmeasured if enabled before their return. Queued/in-flight measurements drain and may produce a final export after off; historical counters do not reset. Once drained, periodic encoding/export stops while off. The worker/control threads and compiled entry/exit probes still exist, so off retains some overhead. Native live control requires LLVM; Python, JavaScript, TypeScript, Java, Go and Rust also provide their language-runtime controls. Live control and cannot add probes, change function filters, enable traces or reload configuration. For short-lived apps, configure the desired startup state.

## Measure the added latency

Run the repeatable comparison with the Collector available:

```sh
make benchmark-live LIVE_BENCHMARK_ARGS='--iterations 50000 --runs 8'
```

The driver builds plain and instrumented binaries from the same unchanged source using the same compiler/flags. Both processes stay alive for all samples. It alternates metrics-off/on ordering in the same instrumented PID, verifies identical checksums, and saves raw samples plus shutdown/loss evidence in `build/benchmarks/live/report.json`. Timing covers the app's batch body, excluding control commands, pipe round trips, startup and final shutdown.

| Comparison | What it measures |
| --- | --- |
| Plain vs live metrics off | Remaining compiled-probe/runtime overhead |
| Live metrics off vs on | Added collection/aggregation/export activity for this workload |
| Plain vs live metrics on | Total instrumentation overhead |

`metrics_added_ns_per_call` is `(median_on - median_off) / calls_per_batch`. Raw elapsed times and percentages are also recorded. Results depend on hardware, workload, optimisation and background activity; short/cheap functions show larger percentage changes. In-flight overlap is tested separately; the benchmark hot function completes synchronously within each batch. A checksum mismatch, loss, incomplete drain or rejected export fails the benchmark instead of reporting equivalent telemetry.

## Regression checks and support boundary

```sh
make developer-examples
make check
```

`developer-examples` builds these four native demonstration apps and their manifests. The tests run plain/instrumented C/C++ comparisons with an upstream OTLP decoder, check original source bytes and exercise toggles across exceptional calls. `make check` also enforces the 80% product line-coverage gate. The local lane remains a prototype; see [support](support.md) before applying it to LTO, shared libraries, asynchronous execution or other platforms.

For source-free Go spans, the [Go span guide](go-spans.md) shows unchanged and annotated sources, generated compiler input, commands and Collector/Tempo viewing. Live metric controls leave the launch-time trace policy enabled.

For unchanged and already annotated C applications with sampled function spans, see [C spans](c-spans.md) and `examples/c-traces.toml`. Function metrics controls operate independently of tracing.
