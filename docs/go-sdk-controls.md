# Go SDK metric controls

This component wraps the pinned official OpenTelemetry Go metric API/SDK 1.47.0. It supplies separate live collection and emission switches for an isolated otelc-owned push pipeline. It does not yet supply a public Go launch/control command or a source-free application adapter. `plan` continues to list Go as remaining.

## Runtime candidate dependencies

The separate owned-runtime candidate pins `golang.org/x/net v0.60.0`, which fixes HTTP/2 advisories GO-2026-6603, GO-2026-6610, GO-2026-6611, GO-2026-6612 and GO-2026-6617. Its dependency locks retain the verified module checksums. Existing application dependency resolutions must still be preserved; an incompatible version requires an explicit build refusal rather than a silent upgrade.

The module graph also contains `x/crypto`, whose OpenPGP packages are [unmaintained and unsafe at every version](https://pkg.go.dev/vuln/GO-2026-5932). The current SDK's compiled package graph does not import those packages. `make go-check` retains the actual `go list -deps` inventory in `build/go-dependencies.txt` and refuses any OpenPGP import, including its subpackages. No advisory suppression is configured. This package-level check is separate from an online advisory scan and does not prove absence of other vulnerabilities.

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

The target checks formatting and `go vet`, runs real SDK tests with the race detector and locked module requirements, and requires at least 80% statement coverage overall and for each production file. It checks the control/pipeline/probe/runtime packages, original-source inspector and [HTTP glue](go-http.md). It writes `build/go-coverage.out` and a combined repository-relative `build/go-sonar-coverage.out` for [Sonar's Go coverage importer](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/test-coverage/go-test-coverage). `make check` includes these components.

Tests cover all four initial states, cached synchronous instruments, constructor and registered observable callbacks, callback metadata/errors/unregistration, disabling during a callback, real periodic-reader retention, application-provider isolation, export cancellation/barriers, exporter errors/shutdown and trace continuity. A delta-temporality negative control loses the paused counter observations; cumulative mode must preserve them.

## Remaining Go integration

The [source candidate](go-source.md) now connects the official compile-time rule engine and these controls to common TOML selection, private builds, public owned launches, function/resource/GC probes and paired benchmarks. The [HTTP candidate](go-http.md) adds the official client/server wrapper and recording-span accounting. Broader source/dependency profiles, independent oteleq qualification, final gates and protected delivery remain required. The SDK control foundation alone does not establish language completion.
