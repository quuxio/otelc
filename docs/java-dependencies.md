# Java dependency and parser compatibility

<!-- markdownlint-disable MD013 -->

otelc uses the official checksum-pinned Java agent and reuses the official patched trace-state parser. Developers do not change application source or add instrumentation dependencies. `make java-build` builds the extension and a separate bootstrap parser JAR; the launcher applies both automatically.

## Qualified versions

| Component | Version or identity |
| --- | --- |
| Official agent | 2.32.0; SHA-256 `f787eb6c7f3d18e69a431e108a15278d25ee37f83d68b678f621e063f3988f82` |
| Agent's embedded SDK | 1.66.0; the W3C parser is replaced as described below |
| Extension compile/test SDK | 1.67.0, including its matching stable/incubating API modules through the official BOM |
| Agent extension/instrumentation SPI | 2.32.0-alpha |
| Official parser backport | `io.opentelemetry:opentelemetry-api:1.67.0`, one relocated class |
| Relocated parser SHA-256 | `bb56f27410ee111a92ab5c172eacb136f1e69795d4e84906a036d7d3b1b42712` |

The compile/test SDK and the agent's embedded SDK are distinct. This is a pinned combination which must pass the real-agent integration checks, including cached instruments, all four same-process metric states, exceptions, lifetimes, decoded OTLP and bounded SDK health. It does not claim that the whole agent has SDK 1.67.0. Future upgrades must repeat those checks, especially for the pinned internal meter-configuration bridge.

## Why the bootstrap parser exists

[GHSA-ccrj-gw8r-854m](https://github.com/open-telemetry/opentelemetry-java/security/advisories/GHSA-ccrj-gw8r-854m) affects the W3C trace-state parser through API 1.66.0; API 1.67.0 fixes it. On 21 October 2026, the published agent and extension API were still 2.32.0 / 2.32.0-alpha. Updating Maven SDK dependencies alone does not replace the API embedded inside that agent.

The build uses Maven Shade to take only the official `W3CTraceContextEncoding.class` from API 1.67.0 and relocate its API references into the agent's existing shaded namespace. It does not reimplement the parser or modify the original official agent JAR. The separate bootstrap JAR contains that class plus its Apache licence and attribution. Its upstream version is pinned separately from ordinary SDK upgrades so the recorded provenance cannot silently change.

The launcher requires the exact relocated class hash, the expected archive entries and bounded archive/class sizes. Missing, changed, duplicate or extra entries, unsupported compression and malformed archives fail before application launch. The bootstrap JAR's file identity joins the existing `tooling` list in prepared launch metadata; the plan, 16-field launch shape and health-report schema stay unchanged. Plain launches do not load the agent or bootstrap patch.

## Validation boundary

The security regression launches the actual agent through the prepared API and extracts a trace context with a 131,072-space malformed state header. It checks the loaded parser's exact JAR/member origin, bootstrap loader, bounded execution, valid remote trace ID, valid state encoding, whitespace handling and duplicate-member rejection. Application source/class/tooling preservation, empty application stderr and a final attached-SDK health report are checked. Separate controls refuse altered bootstrap archives.

The retained before/after reproduction runs the affected and patched parser inside the real agent. The affected call exceeded the five-second test deadline; the patched call completed in approximately 1.4 ms on the local test host. This demonstrates the parser defect and fix; it is not a general application benchmark or an instrumentation-overhead measurement.

The patch covers the agent's shaded parser. An application's independent OpenTelemetry dependencies remain part of that application's dependency management. Adopting new instrumentation binaries requires a normal relaunch; metric collection and emission still switch live after launch. Remove the backport when a matched official agent containing the fix is published and passes qualification.
