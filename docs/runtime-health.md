# Java runtime health

<!-- markdownlint-disable MD013 -->

Instrumented Java launches retain a versioned `health.json` in their private runtime directory. The report observes the pinned official SDK pipeline independently of metric collection and emission. It is SDK evidence: exporter API success does not prove Collector acceptance, storage, or complete invocation attribution.

```sh
# Inspect the running process, without changing metric states.
.venv/bin/python -m otelc health --socket /tmp/otelc-RUNTIME/control.sock

# Inspect the retained checkpoint after the process exits normally.
.venv/bin/python -m otelc health --report /tmp/otelc-RUNTIME/health.json
```

For prepared applications, `launch.identity["health_report"]` gives the report path. `launch.identity["policy_sha256"]` and `launch.identity["selection_sha256"]` bind the normalised policy and resolved selection. `selection.json` contains the resolved method, lifetime-type and resource-type selection; its file identity is retained with generated tooling. Plain launches have no health report or resolved-selection digest.

## Report contract

The response to the separate `health` command and the retained file use the same JSON shape. Metric `status` keeps its existing contract. Schema 1 has `kind="otelc.runtime-health"`, `language="java"`, `scope="sdk_pipeline"`, the actual numeric `pid`, two lowercase SHA-256 digests, `phase="running"|"final"`, and boolean `sdk_attached`. Digests use UTF-8 Python `json.dumps(value, sort_keys=True)` with its default separators/ASCII escaping and no trailing newline. The policy value includes validated defaults; the selection value is the object in `selection.json`.

| Field | Meaning |
| --- | --- |
| `lifecycle.flush`, `lifecycle.shutdown` | Outcomes of the final SDK drain: `not_started`, `pending`, `succeeded`, `failed`, `timed_out`, or `unavailable` |
| `span_accounting.processors_observed`, `batch_processors_observed` | Numeric counts of installed processor observers and supported batch processors |
| `span_accounting.sampled_started`, `sampled_ended`, `sampled_active` | Sampled SDK spans observed at processor entry; active is started minus ended |
| `span_accounting.queue_pending`, `queue_dropped` | Unknown while running; derived only under the clean-shutdown conditions below |
| `span_accounting.queue_evidence` | `unavailable` or `derived_after_shutdown` |
| `trace_exports`, `metric_exports` | Exporter observer count, unit, batch starts/successes/failures/pending, and unit starts/successes/failures |
| `downstream_receipt`, `otlp_partial_rejection` | Explicitly `unavailable`; the supported exporter SPI does not establish these facts |

Each export group has numeric `exporters_observed` and `unit` (`spans` or `metric_points`). Its counters are `batches_started`, `batches_succeeded`, `batches_failed`, `batches_pending`, `units_started`, `units_succeeded`, and `units_failed`. An API exception or failed result counts as a failed attempt; an unfinished async result remains pending. Transport-internal retries are not separate API attempts. Metric units count points passed to the exporter, including repeated cumulative snapshots, rather than unique application observations. Intentionally suppressed metric batches are not export attempts.

All activity counters are decimal strings, preserving integer precision for JavaScript consumers. Unknown counters are JSON `null`, never invented zeroes. An installed observer can establish explicit zero even when no observations occur. An absent exporter or processor cannot. A sampling-zero pipeline can therefore report zero sampled spans, while a disabled trace pipeline reports unavailable span/export counters.

`otelc.health.validate()` and `read()` validate shape/version, private-file bounds and types. They do not certify a successful run. Consumers must match PID/digests to the actual launch, require a final attached report, assess lifecycle/failure/pending/loss evidence, and independently validate decoded OTLP receipt. A missing metric series is not evidence of zero loss. Existing lifetime capacity loss remains available through metric `status`; health schema 1 does not claim lifetime completeness or censor counts.

## Shutdown and queue evidence

The extension replaces the SDK's default JVM hook through its [public pinned builder API](https://github.com/open-telemetry/opentelemetry-java/blob/v1.66.0/sdk-extensions/autoconfigure/src/main/java/io/opentelemetry/sdk/autoconfigure/AutoConfiguredOpenTelemetrySdkBuilder.java). It performs a real tracer/meter force-flush and tracer/meter/logger shutdown, with one total `[export].shutdown_timeout_ms` budget (default 10000, valid range 1..1000000). A daemon drain worker also bounds a provider that blocks before returning its async result. Outstanding observer callbacks are awaited within that same budget.

Queue accounting requires successful flush/shutdown, exactly one supported batch processor and one observed trace exporter, successful processor closure, no pending trace export callbacks, no active sampled spans, and consistent counters. Only then is pending zero and `queue_dropped = sampled_ended - trace_exports.units_started`. This counts ended spans that never reached the exporter, including processor-closed rejections; it does not distinguish their individual drop causes. Failed exported spans remain in `units_failed`; they are not counted twice as queue drops. Other processor/exporter combinations, timeout/failure, open spans and inconsistent counts retain `null` queue values.

The final private 0600 file atomically replaces the startup/attached report in a 0700 directory. If the final write fails, the older non-final report remains and must be rejected. Forced termination without hooks, including SIGKILL, cannot supply a final report. JVM application shutdown hooks run concurrently; this is an SDK checkpoint, not proof that application work is quiescent. Later activity cannot rewrite the frozen checkpoint. Per-invocation nonce/probe attribution and general consumer adoption remain separate requirements.

Tests exercise the real agent with metrics initially off, exact decoded receiver counts, async exporter failures, synchronous exceptions, a blocked export and overflowing real batch queue, unsampled/open spans, unsupported processors, private-file failures and bounded flush/shutdown timeouts. No runtime performance budget is established by these correctness tests.
