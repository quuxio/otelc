# Go SDK metric controls

This component wraps the pinned official OpenTelemetry Go metric API/SDK 1.47.0. It supplies separate live collection and emission switches for an isolated otelc-owned push pipeline. It does not yet supply a public Go launch/control command or a source-free application adapter. `plan` continues to list Go as remaining.

## Behaviour

Create the controls and owned SDK pipeline before the application starts, even when metrics are initially off. Cached meters, synchronous instruments and observable registrations then follow later changes without provider replacement or an application restart.

| Collection | Emission | Owned pipeline behaviour |
| --- | --- | --- |
| off | off | Ignore new measurements/callbacks and stop export |
| off | on | Ignore new measurements/callbacks; previously collected cumulative data can export |
| on | off | Collect measurements; retain cumulative totals without exporting |
| on | on | Collect and export |

`SetCollection` changes future measurement/callback admission. A synchronous measurement or callback already admitted can finish. Callback observations recheck the current switch before recording. Collection-off prevents the configured observable callbacks from running; emission-off alone does not prevent callbacks or SDK aggregation.

`SetEmission` takes a context and waits for an in-flight owned export or flush before acknowledging its new state. A cancelled or expired request returns an error and leaves the switch unchanged. A caller must inspect that error. Re-enabling does not immediately push data: the next SDK collection/export or explicit flush makes it visible. Export failures propagate to the SDK; shutdown still closes the underlying exporter exactly once, including while emission is off.

The wrapper requests cumulative temporality for owned metrics. This retains counter/histogram measurements collected during an emission pause, rather than discarding delta snapshots. Gauge values retain the SDK's latest-value meaning; they are not a history of every sample. Restoring emission does not recreate observations ignored while collection was off.

Owned meters must use a separate SDK provider and exporter. The provider router sends scopes selected by the adapter to that owned provider; other scopes go directly to the original application provider. The component does not install a global provider. Application-owned meters therefore retain their original callbacks, export behaviour and temporality. Tracing uses its separate SDK pipeline and is independent of both switches.

## Component checks

Use Go 1.27.3, the version pinned in CI, with CGO and a C compiler for the race detector. The module requires at least Go 1.26.0.

```sh
make go-check
```

The target checks formatting and `go vet`, runs real SDK tests with the race detector and locked module requirements, and requires at least 80% statement coverage overall and for each production file. This includes the three control files and four candidate [owned pipeline files](go-source.md#owned-sdk-export-scheduling). It writes `build/go-coverage.out` and a repository-relative `build/go-sonar-coverage.out` for [Sonar's Go coverage importer](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/go-test-coverage). `make check` includes these components.

Tests cover all four initial states, cached synchronous instruments, constructor and registered observable callbacks, callback metadata/errors/unregistration, disabling during a callback, real periodic-reader retention, application-provider isolation, export cancellation/barriers, exporter errors/shutdown and trace continuity. A delta-temporality negative control loses the paused counter observations; cumulative mode must preserve them.

## Remaining Go integration

The official `go.opentelemetry.io/otelc` compile-time rule engine can weave generated hooks into a private build without editing original application files. Connecting that engine and these SDK controls to the common TOML still requires a locked build recipe, selected-function matching/refusal, public launch and control ownership, official library/context integration, resource/GC observations, OTLP accounting, equivalent application benchmarks, Grafana evidence and independent oteleq qualification. This component does not establish those capabilities.
