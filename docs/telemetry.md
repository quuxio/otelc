# Telemetry semantics

The local implementation exports callback timing metrics. Traces and context integration below remain future contracts; see [current implementation](local-implementation.md).

## Metrics

The initial worker exports aggregate function timing as cumulative OTLP metrics. These are project-specific instrument names, not claims of standardized OpenTelemetry function-metric conventions.

| Instrument | Type / unit | Meaning |
| --- | --- | --- |
| `otelc.function.calls` | Monotonic sum / `{call}` | Completed selected invocations received by the worker |
| `otelc.function.duration` | Explicit-bucket histogram / `s` | Inclusive duration of those completed invocations |
| `otelc.runtime.dropped_observations` | Monotonic sum / `{observation}` | Known admission, queue, stack, or invalid-exit losses, grouped by bounded reason |
| `otelc.runtime.dropped_traces` | Monotonic sum / `{trace}` | Sampled trees discarded because they are incomplete or exceed bounds |
| `otelc.export.dropped_batches` | Monotonic sum / `{batch}` | Batches discarded after queue or retry limits |

The first two metrics describe observed completions, not every attempted application call. They represent all selected normal-return calls only when relevant runtime loss counters are zero and the execution model stays inside support. Never scale them by a trace sampling ratio. A queue drop loses both that call's count and its duration measurement; the independently exported loss counter exposes the gap.

Histograms have fixed, sorted positive bucket boundaries, count, sum, and cumulative start time. Cumulative temporality preserves aggregate values across intermittent export failures while the process is alive. Use a new start timestamp after restart, never merge different process lifetimes as one accumulation. See the [OpenTelemetry metric data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/).

Call duration is inclusive of nested application work and probe overhead. Exclusive/self time is deferred until child timing and unsupported-exit handling have a proven contract. Percentiles are backend calculations from histogram buckets, not exact quantiles produced by the callback runtime.

## Identity and cardinality

Resources identify `service.name`, an optional `service.version`, and a process-specific `service.instance.id`. The instrumentation scope identifies quux otelc and its version. Function metrics use a bounded manifest-derived `code.function.name` attribute and module identity where needed to avoid ambiguity. Follow the applicable [code semantic conventions](https://opentelemetry.io/docs/specs/semconv/attributes-registry/code/) for attributes; these conventions do not define this project's custom metric names.

Source path and line are optional span metadata, disabled as metric dimensions. Paths default to project-relative values. Raw addresses, caller addresses, thread IDs, invocation tokens, request IDs, arguments, return values, and exception messages are never metric labels. Template instantiations can produce many distinct names, so selected-function cardinality is validated before enabling telemetry.

## Traces

Traces become supported only with an exit-capable backend. The nearest admitted selected invocation is the parent. Unselected functions are absent from the tree. A selected invocation with no selected parent begins a process-local root; ordinary function callbacks alone do not establish HTTP requests or distributed trace context.

Sampling is decided once at a selected root and inherited by its selected descendants. The decision affects traces only; metric observations continue independently. The worker builds sampled trees from completed invocation records using root/parent tokens, then generates nonzero OTLP trace and span IDs. IDs do not derive from raw function addresses.

A trace requires a root completion marker, the expected admitted-completion count, and independent loss/generation bookkeeping. Queue loss, unsupported exit, retirement, a missing parent, a time limit, or a span-count limit invalidates the entire sampled tree. Discard it and increment diagnostics. Do not attach orphan spans to an unrelated surviving parent or hold partial trees indefinitely. The C LLVM backend implements the required root-entry/completion markers and admitted-completion bookkeeping in primitive records; see [C spans](c-spans.md). A fixed timing record alone is insufficient for trace completeness.

Completed spans carry a display name, duration-derived start/end timestamps, optional code metadata, and an internal backend marker. Ordinary returns leave status unset. A supported escaping exception produces an error outcome; detecting a nonzero integer return does not imply a business error. An exception caught within the function does not make its eventual normal exit an error. See the [OpenTelemetry tracing API](https://opentelemetry.io/docs/specs/otel/trace/api/).

## Time

Producer durations come from a monotonic clock. A worker-owned monotonic/realtime anchor converts starts to Unix nanoseconds; end time is start plus measured duration. One trace pins an anchor generation. Wall-clock corrections and suspend/resume behaviour are tested separately, and negative or overflowing conversions invalidate the observation instead of emitting impossible timestamps.

## Export

The first transport is OTLP/HTTP with protobuf, supporting a local Collector endpoint and explicit HTTPS endpoints. Export runs in batches with bounded queues and retry budgets. Authentication headers are never logged. OTLP partial success must be interpreted using the protocol response, including rejected item counts; it must not trigger blind retries of accepted data.

Retry transient errors within the bounded budget, then account for discarded batches. Protocol-compliant retries do not guarantee exactly-once ingestion. Document possible duplicate batches after an ambiguous transport failure. Worker/exporter failure cannot block producer threads. See the [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).

## Context and future signals

Automatic cross-thread, task, or cross-process parenting requires explicit context handoff or a validated language/framework adapter. Until then, threads have independent roots. Manually instrumented OpenTelemetry spans and otelc spans are not automatically joined merely because they share a process.

An adapter can later capture/attach a context at known boundaries without changing the minimal probe ABI. Error metrics, exemplars, exclusive timing, and profiles are deferred until each has a supported semantic contract. The initial product should report a few honest metrics rather than infer signals it cannot observe.

C++ LLVM spans use the native private SDK path with escaping Itanium unwind status. Distinct selected ABI bodies sharing a demangled name receive separate linkage labels; configuration uses their original demangled name. These are function-body spans, not object lifetimes. See [C++ spans](cpp-spans.md).
