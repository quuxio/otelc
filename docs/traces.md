# Function traces and spans

C, C++, Rust, Python, Java, JavaScript, TypeScript and Go produce sampled function spans from unchanged application source. Each adapter uses the common schema-2 policy and a private OpenTelemetry SDK. A trace groups selected invocations with parent/child relationships; spans measure individual function bodies or the qualified async completion boundary.

## Enable tracing

Use the supplied `examples/<language>-traces.toml` policy. It selects the unchanged example, names its service and configures the local Collector. The shared trace settings are:

```toml
[traces]
enabled = true
root_sample_ratio = 1.0
max_active_traces = 32
max_spans_per_trace = 256

[export]
endpoint = "http://127.0.0.1:4318"
interval_ms = 500
```

Keep the language, source, function and resource sections from the example policy. Native C/C++ spans require the matched LLVM 22 backend. Sampling applies to whole local trees and does not scale or disable metrics. Live metrics controls do not toggle tracing; trace enablement remains a launch setting. See [common configuration](common-configuration.md) and [signal-specific export settings](rust-spans.md#independent-metrics-and-trace-settings).

| Language | Run, configuration and qualified boundaries |
| --- | --- |
| C | [LLVM function spans](c-spans.md) |
| C++ | [LLVM function bodies and exceptional exits](cpp-spans.md) |
| Rust | [Synchronous and directly awaited async spans](rust-spans.md) |
| Python | [Interpreter frames, coroutines and generators](python-spans.md) |
| Java | [Bytecode method-body spans](java-spans.md) |
| JavaScript | [Node function and Promise completion spans](javascript-spans.md) |
| TypeScript | [Compiler emission and Node spans](typescript-spans.md) |
| Go | [Compiler overlays and same-goroutine spans](go-spans.md) |

## Verify the complete local pipeline

Install the [development toolchains](../README.md#working-on-this-repository) and start the [Collector, Tempo and Grafana stack](observability-stack.md). From the repository root:

```sh
make stack-up
make trace-check
# Select one language or allow more time for Tempo indexing:
make trace-check TRACE_CHECK_ARGS='--language python --timeout 120'
```

On macOS, if Maven reports a `PKIX` certificate trust failure, the tested JDK 21 can use the Mac's existing Keychain trust provider for this build:

```sh
MAVEN_OPTS='-Djavax.net.ssl.trustStoreType=KeychainStore -Djavax.net.ssl.trustStore=NONE' make trace-check
```

Certificate validation remains enabled. This does not change the system or JDK trust store.

The command builds the adapters, runs the ordinary and instrumented internal examples, compares their outputs and hashes the original source/configuration. It requires successful shutdown, the expected completed function/tree counts and zero reported runtime, trace and export losses. It then queries Tempo for a unique service name per language and verifies persisted span counts, error statuses, nonzero IDs, one root per tree, parent existence, acyclic parenting and child timestamps within their parent. The unique service names isolate this run from old examples.

Expected fixture totals are 7 C, 31 C++, 8 Rust, 9 Python, 10 Java, 11 JavaScript, 11 TypeScript and 10 Go spans: 97 spans in 39 trees. The C++ total includes distinct compiled constructor/destructor ABI bodies; it is not an object count. A clean report or Collector acknowledgement alone does not satisfy the storage check.

Successful results and stored span metadata are written to `build/trace-check/results.json`. Each language prints a Grafana link with its service and a stored trace ID. Per-language runtime reports and generated policies remain under the same directory. A failed run exits nonzero and removes any previous success summary. The current verifier uses the standard loopback Collector, Tempo and Grafana ports; custom ports require a separate verification workflow. It does not start/stop the stack or delete its stored telemetry.

## View and interpret

Open the [read-only trace dashboard](http://localhost:3000/d/otelc-traces). Set **Service** to the configured service name and select a trace, or use the link printed by `make trace-check`. The selected trace shows nested spans, durations and escaping-error status. Arguments, return values and exception messages are not span attributes.

This qualifies function trees within each adapter's documented execution model. Python standard asyncio tasks can inherit submission context with `propagation.tasks=true`; see [the unchanged-source example and scope](python-task-context.md). Other task integrations, threads and services do not yet automatically share a trace. Remote parents, span links, application SDK interoperability, automatic object / resource lifetime spans and production overhead qualification remain separate work under the [all-language implementation plan](context-and-lifetime-plan.md). Metrics-only benchmarks do not measure tracing overhead. See [support boundaries](support.md) before applying the prototype to a new workload.
