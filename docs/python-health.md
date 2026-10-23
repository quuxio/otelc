# Python runtime health

<!-- markdownlint-disable MD013 -->

The common `health` command now reads the Python candidate's application/SDK evidence. It does not change metric switches, force a collection/export or drain queued lifetime observations. Developer sources need no changes. Java keeps its existing version-one report; Python uses version two because its application and SDK run in separate processes and some observations have different scopes.

For a running prepared Python launch, use its private control descriptor:

```sh
bin/quux-otelc health --socket /absolute/runtime/control.json
```

After the owning supervisor finishes, inspect its retained checkpoint:

```sh
bin/quux-otelc health --report /absolute/runtime/health.json
```

An instrumented launch publishes `health.json` once, after application/SDK cleanup. A plain launch has no telemetry health report. An existing file is never overwritten. Publication failure is retained in the execution record and causes a failed supervisor result while preserving the application's actual exit and native output. Startup failure can leave unavailable identities as `null`; an already observed failed SDK PID/exit is retained.

## Version-two evidence

The report has `kind=otelc.runtime-health`, `language=python`, `scope=application_sdk_pair` and `phase=running|final`. It binds actual application/SDK PIDs, the random SDK launch ID and the SDK's compact canonical common-policy SHA256. Its nested SDK report must have those same identities. This policy digest follows the existing Python wire contract; it is not the differently serialised Java version-one digest or a resolved-code selection index.

| Field | Evidence |
| --- | --- |
| `trace_exports` | Actual SDK exporter batch and span attempts, successes, failures and pending batches |
| `metric_exports` | Actual SDK exporter batch attempts, successes, failures and pending batches; metric-point counts remain `null` |
| `metric_flush` | Exporter-level flush attempts/failures, explicitly scoped as `exporter` |
| `lifecycle.flush` | `unavailable`; the existing observations do not establish one aggregate application/trace/meter flush outcome |
| `lifecycle.shutdown` | Final SDK shutdown outcome when observed; failure does not distinguish timeout from an exporter exception |
| `span_accounting` | Sampled application-processor counts when installed, plus the SDK's separately derived post-shutdown queue evidence |
| `application` | Live application counters or final producer checkpoint, including loss, handoff and lifetime completion/censoring evidence |
| `sdk` | Original SDK status/checkpoint, preserving operation/protocol/reply failures and pending work |
| `application_exit`, `sdk_exit`, `failures` | Observed final exits and supervisor evidence failures; unavailable while running |

Normalised activity counts are decimal strings so readers do not lose integer precision. Unknown counts and absent observers stay `null`. The raw nested Python records retain their existing integer wire encoding. A zero is an observed zero; it is not a substitute for missing evidence.

Live application and SDK observations are separate snapshots, rather than one atomic instant. `application.lifetime_events_pending` reports queued GC/type events that the health query leaves untouched. Live lifetime counts describe the processed observations at that snapshot; they may lag the queued events. The existing metrics `status` command retains its observation-barrier behaviour. Health does not run garbage collection or complete/censor lifetimes.

Final `phase=final` means this is the retained owner checkpoint, not a clean-health verdict. Inspect failures, exits, SDK phase, pending work, queue loss, handoff loss and censored lifetimes. The SDK queue estimate counts spans received by that SDK; application-side losses remain separate. Successful shutdown does not erase earlier export failures. The `health` reader validates shape and identities, not successful application behaviour, source closure or telemetry delivery.

`downstream_receipt` and `otlp_partial_rejection` remain `unavailable`, and `source_complete=false`. Validate actual decoded Collector/Prometheus/Tempo receipt separately. This health view does not complete rebuilt-provider oteleq admission.

The fixed `health.json` reader retains private owned-file/directory checks. Version-two reports and `health` responses have a 64 KiB bound. Java version-one files keep their 4 KiB bound, and ordinary metric control responses remain limited to 4 KiB. Duplicate JSON keys/non-finite values in retained reports are refused.

Focused checks cover a real same-process metric-off control endpoint, active traces, queued weak-GC events, actual public launcher/CLI commands, final failure reporting and publication conflicts:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_health test_python_prepared test_control test_health -v
make check
```
