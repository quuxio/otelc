# Configuration proposal

## Format and status

The proposed format is TOML, shared by build-time selection and runtime policy. [examples/otelc.toml](../examples/otelc.toml) is the complete draft example. There is no configuration parser or installed CLI yet.

`schema_version = 1` is required. Unknown keys, unsupported backend features, invalid values, and patterns selecting more functions than the configured bound are errors. Environment interpolation is limited to specifically documented OTLP settings; arbitrary shell expansion and executable configuration hooks are excluded.

## Selection rules

| Section | Purpose |
| --- | --- |
| `build` | Choose a backend and supported application source paths |
| `functions` | Select demangled function names from the build manifest |
| `runtime` | Set capacity and shutdown budgets |
| `metrics` | Configure aggregation and histogram boundaries |
| `traces` | Enable a supported trace backend and coherent sampling |
| `export` | Configure OTLP transport and batching |
| `resource` | Supply service identity and approved resource attributes |

Paths match normalized project-relative paths using `/`. Leading `**/` covers arbitrary directory depth. Function patterns match the complete demangled display name using `*` for any substring and `?` for one character. Exclusions take priority. An empty include list selects nothing; it must never mean "instrument everything".

Default runtime exclusions cover runtime functions, the exporter, and non-application metadata. Example exclusions such as `std::*` are useful but do not prove that every compiler-generated or third-party function is excluded. `inspect` reports included/excluded symbols and the rule responsible. Selection is evaluated against the manifest, not by demangling callback addresses at runtime.

Configuration loading uses, in order, defaults, the selected TOML file, and documented environment overrides. `--config` chooses the file; otherwise the wrapper uses `otelc.toml` at the project root. Runtime launch receives an explicit configuration and manifest path. No implicit network configuration lookup occurs.

## Draft defaults and validation

| Setting | Default | Validation |
| --- | --- | --- |
| Backend | `callbacks` | Recognized and supported by the exact compiler |
| Maximum selected functions | 4096 | Positive, bounded; do not silently aggregate unrelated functions |
| Maximum admitted threads | 128 | Positive and within the platform's tested budget |
| Stack depth | 256 | Positive; bounded overflow suppression |
| Queue capacity per thread | 4096 | Power of two, at least 64 |
| Shutdown deadline | 2000 ms | Positive and bounded |
| Metrics | Enabled | Completed observations independent of trace sampling |
| Export interval | 5000 ms | Positive |
| Traces | Disabled | Callback backend rejects `enabled = true` |
| Root sample ratio | 0.01 | Between 0 and 1; applied to whole traces |
| Maximum active traces | 512 | Positive; only allocated when supported tracing is enabled |
| Maximum spans per trace | 1024 | Positive; exceeding it discards the trace with diagnostics |
| Transport | `http/protobuf` | Initial supported OTLP transport |
| Endpoint | `http://localhost:4318` | HTTP allowed only for explicitly local development; use HTTPS remotely |
| Maximum queued export batches | 8 | Positive and bounded |
| Export timeout | 1000 ms | Positive; application threads never wait on it |

Capacity validation includes checked arithmetic for the calculated memory budget. A contradictory configuration, such as tracing enabled on the normal-return callback backend, fails before compilation or runtime startup.

## OTLP settings and secrets

The runtime will recognize `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and supported signal-specific endpoint, timeout, protocol, and header settings. Values must follow the [OTLP exporter configuration specification](https://opentelemetry.io/docs/specs/otel/protocol/exporter/). A signal-specific endpoint is used as given; a base HTTP endpoint has `/v1/metrics` or `/v1/traces` appended according to the specification.

Authentication headers are supplied through the supported OTLP environment or secret-file integration when implemented. They never appear in manifests, `inspect` output, logs, example configuration, or metric attributes. Avoid committing secret-bearing environment files. Invalid credentials affect export diagnostics, not application execution.

## Planned CLI

| Command | Contract | Milestone |
| --- | --- | --- |
| `doctor` | Report compiler, target, backend capabilities, clocks, and runtime availability | M1 |
| `clang` / `clang++` wrapper | Preserve normal driver behaviour while adding supported instrumentation | M1 |
| `inspect <binary>` | Verify manifest identity and show selected functions and limits | M1 |
| `run <binary>` | Supply configuration/manifest paths and run with normal signal/exit behaviour | M1 |
| `build` | Integrate a tested build system, starting with CMake | M3 |
| `status` / `enable` / `disable` | Inspect or change admission policy on an already instrumented process | M3 |

The released executable is intended to be called `quux-otelc`. These are proposed commands; `make help` describes the commands actually available in this repository today.

## Runtime updates

M1 configuration is immutable for a process lifetime. M3 may publish a new immutable admission generation over an owner-only local control socket. Each accepted frame retains its entry policy until it leaves, so disabling a function does not orphan its open frames. Rejected entries remain rejected even if a new policy enables that function before their exit.

Runtime enable/disable only affects probes compiled into the executable. It cannot add missing probes. Callback overhead remains when telemetry admission is off; dynamic machine-code patching is not part of the initial control design.
