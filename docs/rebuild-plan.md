# Rebuild and acceptance criteria

<!-- markdownlint-disable MD013 -->

The previous main implementation is preserved on [archive/pre-rebuild-2026-10-21](https://github.com/quuxio/otelc/tree/archive/pre-rebuild-2026-10-21), commit `c4af0bd18b668b0dbdb42bf0b08353b23b9bfd7c`. Main is replaced through protected reviewed commits, preserving ancestry and unmerged work. Reuse proven components from the archive where they help.

## Required behaviour

One `examples/otelc.toml` applies unchanged to equivalent Java, TypeScript/JavaScript, Python, Go, C, C++ and Rust example applications. LLVM is a compiler-backend milestone, with an IR example to qualify it independently. Developers provide only configuration and a build/launch command: no instrumentation imports, annotations, decorators, guards or hand-written bootstrap. Metric collection and emission switch independently and together inside the same process, including enabling from initially off. Tracing stays independent.

Object lifetimes are selected through the common policy. Report construction-to-destruction/drop, construction-to-observed-GC and resource-close boundaries separately. No fabricated destruction times, forced production collection or strong retention of application objects. Report uncompleted/censored observations and bounded capacity losses.

## Delivery order

1. Java: official OpenTelemetry Java agent, common-policy translation and small extension for direct function metrics, live controls and lifetime observation.
2. Microsoft TypeScript and JavaScript: official Node SDK/instrumentations, generated loader/bootstrap and precise application probes; qualify both Microsoft compiler paths and CJS/ESM.
3. Python: official SDK/library instrumentors with CPython monitoring and bounded lifecycle observation.
4. Go: official compile-time rule engine and SDK, generated rules and metric-only live controls.
5. LLVM: selectively reuse the archive's exception-aware pass and manifest/runtime contract; independent IR fixture.
6. C: equivalent workload and selected resource acquisition/release observations.
7. C++: equivalent exception-enabled workload, complete-object construction/destruction and resource observations.
8. Rust: equivalent workload, compiler/Cargo integration, ownership/drop and qualified async cleanup.

Do not claim a language complete until its source/outcome preservation, exact positive/negative selection, decoded telemetry, same-process live control, lifecycle boundaries, independent coverage floors, review and required CI/Sonar gates pass.

## Cross-language performance comparison

Every application implements the same seeded integer workload: 128 steps of `x = (x * 1664525 + 1013904223) & 65535`, accumulating each step into an integer result. Each batch calls `workload(i)` for `i` from zero to count minus one and accumulates the results without overflow. Validate checksums independently and across languages before retaining timings. Allocate one ordinary `WorkItem` per invocation for the lifetime example; no telemetry code belongs in an example.

Keep plain and instrumented processes alive through warmup and repeated samples. Exercise all four live states in the same instrumented process. Retain raw nanoseconds per operation, sample count, checksums, version/build flags, host identity, state transitions and loss evidence. Alternate enabled/disabled order. A plain run measures the total installed-instrumentation cost; disabled-probe measurements do not replace it.

Grafana must show absolute per-language ns/op and throughput, plus each language's overhead relative to its own plain baseline. Label runtime/compiler, optimisation, workload version, mode and run identity. Never present a sampled function histogram as exact all-call benchmark evidence. Compare lifetime durations only within compatible boundary labels. Record separate lifecycle-observer overhead.

## Observability stack

Compose provides an OTLP/HTTP Collector, Prometheus, Tempo and provisioned Grafana dashboards. The rebuild uses a distinct project and loopback ports so the pre-existing stack and its data remain available: OTLP 4320, Grafana 3001, Prometheus 9091 and Tempo 3201. Validate actual received metrics, scrape results and dashboard queries for every delivered language; running containers alone is not delivery.

Collector batches have a hard maximum of 512 items and send at 256 items or after one second. The unbounded default allowed batches of 8192 rich lifetime spans to exceed Tempo's internal 16 MiB request limit, causing 429 retries and drops. Qualification must cover repeated lifetime-span bursts and check downstream receipt and Collector errors, not only HTTP acceptance at the receiver.

## Focused failure cases

Test stale/invalid policy, unsupported selectors, overloads and exclusions; cached and newly-created instruments; observable callbacks while collection is off; outstanding calls and GC/release around a toggle; SDK export retries and shutdown with an open control connection; module/loader/compiler differences; constructor failure, duplicate close, address/identity reuse and incomplete lifetimes. Preserve source/module hashes, application values and exceptions. Record unsupported boundaries explicitly.
