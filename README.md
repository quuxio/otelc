<p align="center">
  <img src="docs/assets/quux-mark.png" width="160" alt="quux">
</p>

# otelc

**Compiler-assisted observability for native applications. A [quux](https://quux.io) project.**

[![CI](https://github.com/quuxio/otelc/actions/workflows/ci.yml/badge.svg)](https://github.com/quuxio/otelc/actions/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-design-blue)](docs/roadmap.md)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

---

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=bugs)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=code_smells)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=coverage)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Lines of Code](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=ncloc)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Technical Debt](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=sqale_index)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=quuxio_otelc&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=quuxio_otelc)
[![Repo Traffic](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fquuxio%2Fotelc%2Fmain%2F.badges%2Ftraffic.json&cacheSeconds=3600)](https://github.com/quuxio/otelc)

---

## What is otelc?

otelc is a design for automatic function telemetry in native applications. Rebuild selected application code with compiler probes, link a small Rust runtime, and export function timing and, later, traces through OpenTelemetry Protocol (OTLP). Application source stays unchanged; rebuilding and linking the runtime are required.

The first target is synchronous C and C++ on macOS ARM64 and Linux x86-64/ARM64. The architecture separates compiler integration, a language-neutral probe ABI, a bounded runtime, and telemetry export so additional native languages can be added through validated adapters.

**Current status: design and repository tooling. The compiler wrapper, runtime, LLVM pass, and OTLP exporter are not implemented yet.** The examples below describe the intended interface. Quality and coverage badges currently measure repository tooling, not an instrumentation runtime.

## How it will work

```mermaid
flowchart LR
    Source[Selected C/C++ source] --> Compiler[Clang + compiler probes]
    Compiler --> App[Native executable]
    App --> Runtime[Rust runtime: bounded thread buffers]
    Runtime --> Worker[Aggregate metrics / sample traces]
    Worker --> Collector[OTLP Collector]
    Collector --> Backend[Your observability backend]
```

The callback backend will first prove normal-return function timing using Clang's `-finstrument-functions`. An LLVM pass will then add compile-time function selection, compact metadata, and exceptional-exit handling before nested traces become a supported feature. Timing, symbol resolution, error semantics, and data loss are explicit parts of the [design](docs/design.md).

## Intended workflow

The proposed executable name is `quux-otelc`, avoiding a command collision with OpenTelemetry's existing Go `otelc`. These commands are planned, not available in this checkout:

```sh
# Inspect the toolchain and explain its capabilities.
quux-otelc doctor

# Compile and link selected code using the callback backend.
quux-otelc --config otelc.toml clang++ -O2 -g -fno-exceptions app.cpp -o app

# Inspect the functions and manifest produced by the build.
quux-otelc inspect ./app

# Run with the chosen configuration and a bounded shutdown flush.
quux-otelc --config otelc.toml run ./app
```

The initial C++ timing contract requires selected translation units to already use `-fno-exceptions`. The wrapper will reject unsupported configurations rather than silently change application semantics. Exception-enabled C++ is a requirement of the LLVM milestone. See [compiler integration](docs/instrumentation.md) and [support boundaries](docs/support.md).

## Design documentation

Start with the [documentation index](docs/README.md) or [system design](docs/design.md).

| Document | Contents |
| --- | --- |
| [System design](docs/design.md) | Scope, architecture, components, and invariants |
| [Compiler integration](docs/instrumentation.md) | Callback prototype, metadata, build integration, and LLVM pass |
| [Probe ABI](docs/abi.md) | Draft C interface, descriptors, lifecycle, and compatibility |
| [Runtime](docs/runtime.md) | Thread buffers, recursion, memory bounds, overload, and shutdown |
| [Configuration](docs/configuration.md) | TOML schema, selection rules, defaults, and planned CLI |
| [Telemetry](docs/telemetry.md) | Metric semantics, trace construction, timestamps, and OTLP |
| [Support matrix](docs/support.md) | Planned languages, platforms, and unsupported execution models |
| [Roadmap and validation](docs/roadmap.md) | Milestones, acceptance criteria, tests, and performance evidence |
| [Design decisions](docs/decisions.md) | Decisions and alternatives |
| [Related work and compiler checks](docs/research.md) | Primary sources and reproducible feasibility checks |
| [Repository quality](docs/quality.md) | Local checks, CI, SonarQube, and badge maintenance |

## Working on this repository

Python and Node.js are used only for repository validation; the product implementation is planned in Rust, with a small native shim and LLVM C++ pass. Install Python 3.11+, Node.js 24+, and Make, then run:

```sh
make setup
make check
make help
```

`make check` lints Markdown and runs the SonarQube policy helper's tests with a minimum 90% line coverage. `make scan` verifies the remote new-code policy and scans a clean Git commit when `SONAR_TOKEN` is available. See [quality setup](docs/quality.md) for the current analysis scope and future Rust gates.

## Contributing

Design feedback and implementation proposals are welcome through [issues](https://github.com/quuxio/otelc/issues) and [discussions](https://github.com/quuxio/otelc/discussions). Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [milestones](docs/roadmap.md) before starting a change. Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## License

Released under [AGPL-3.0](LICENSE). No separate runtime linking exception is granted by this repository.
