<p align="center">
  <img src="docs/assets/quux-mark.png" width="160" alt="quux">
</p>

# otelc

**Compiler-assisted observability for native applications. A [quux](https://quux.io) project.**

[![CI](https://github.com/quuxio/otelc/actions/workflows/ci.yml/badge.svg)](https://github.com/quuxio/otelc/actions/workflows/ci.yml)
[![Status](https://img.shields.io/badge/status-local%20prototype-blue)](docs/roadmap.md)
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

otelc provides a local native timing prototype for automatic function telemetry in native applications. Rebuild selected application code with compiler probes, link a bounded Rust runtime, and export function timing through OpenTelemetry Protocol (OTLP). C/C++, Rust, Python, Java, JavaScript, TypeScript and Go also support sampled function spans; see [C spans](docs/c-spans.md), [C++ spans](docs/cpp-spans.md), [Rust spans](docs/rust-spans.md), [Python spans](docs/python-spans.md), [Java method spans](docs/java-spans.md), [JavaScript spans](docs/javascript-spans.md) and [TypeScript spans](docs/typescript-spans.md) and [Go spans](docs/go-spans.md). The product goal is instrumentation without application source edits across C, C++, Rust, TypeScript/JavaScript, Java, Python and Go. C/C++, Python, JavaScript, TypeScript, Java, Go and Rust function timing follow this model; the current opt-in C++ lifetime guard requires source edits and does not yet meet the automatic lifetime requirement. Rebuilding/linking or changing the launch command may be required. See the [source-free contract](docs/design.md#source-free-instrumentation-contract).

The first target is synchronous C and C++ on macOS ARM64 and Linux x86-64/ARM64. The architecture separates compiler integration, a language-neutral probe ABI, a bounded runtime, and telemetry export so additional native languages can be added through validated adapters.

**Current status: local native prototype, validated on macOS ARM64.** The compiler wrapper, manifest inspection, bounded runtime, OTLP/HTTP metrics, exception-aware LLVM pass and explicit C++ object lifetime guard are implemented locally. Existing Clang function annotations, live LLVM metrics on/off controls and unchanged-source Python function timing are also implemented. Python uses CPython 3.12+ monitoring and the Python OpenTelemetry SDK. JavaScript uses an in-memory Node module transform and the JavaScript SDK; see its [101 guide](docs/javascript.md). TypeScript uses compiler emission, original source maps and the same SDK; see its [101 guide](docs/typescript.md). Java uses a JDK agent and bytecode body probes; see its [101 guide](docs/java.md). Go uses compiler overlays and the Go SDK; see its [101 guide](docs/go.md). Rust uses private generated source and a Cargo compiler wrapper with the Rust SDK, including qualified async completion and cancellation timing; see its [101 guide](docs/rust.md). See the [language adapters](docs/languages.md). Live filter changes and additional platform qualification remain planned. Published quality badges cover the repository-tooling SonarQube analysis; native coverage is enforced separately by the product quality gate. See the [local implementation and validation](docs/local-implementation.md).

## How it will work

Dependabot alerts and security updates are enabled separately from weekly dependency-update proposals. Run `make github-security-check` to verify the live settings and open-alert count as quuxio; see the [security policy](SECURITY.md#dependabot-alerts-and-security-updates).

```mermaid
flowchart LR
    Source[Selected C/C++ source] --> Compiler[Clang + compiler probes]
    Compiler --> App[Native executable]
    App --> Runtime[Rust runtime: bounded thread buffers]
    Runtime --> Worker[Aggregate metrics / sample traces]
    Worker --> Collector[OTLP Collector]
    Collector --> Backend[Your observability backend]
```

The callback backend provides normal-return function timing using Clang's `-finstrument-functions`. The matched LLVM 22 backend provides compile-time function selection and C++ exceptional-exit timing. Sampled C/C++ function-body traces are available with the LLVM backend. Timing, symbol resolution, error semantics, and data loss are explicit parts of the [design](docs/design.md).

## Local workflow

The proposed executable name is `quux-otelc`, avoiding a command collision with OpenTelemetry's existing Go `otelc`. Build the CLI and runtime together, then use the supplied local fixture configuration:

```sh
make build
mkdir -p build/native
./target/debug/quux-otelc --config examples/local.toml doctor
./target/debug/quux-otelc --config examples/local.toml clang -O2 -g \
  tests/fixtures/timing.c -o build/native/timing
./target/debug/quux-otelc inspect build/native/timing
./target/debug/quux-otelc --config examples/local.toml run ./build/native/timing
```

For exception-enabled C++, use the LLVM backend in `examples/exceptions.toml`; `make examples` builds internal C, C++, exception and object lifetime apps. The legacy callback backend requires selected C++ translation units to already use `-fno-exceptions`. Start the [Docker Collector, Prometheus and Grafana stack](docs/observability-stack.md) with `make stack-up` to view metrics. See [compiler integration](docs/instrumentation.md) and [support boundaries](docs/support.md).

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

The implementation uses Rust, a small C shim and a C++ LLVM pass. Install Rust 1.98+, matched LLVM 22/Clang (`brew install llvm@22` on this Mac), Python 3.12+, Node.js 24.11+ and Make, then run:

```sh
make setup
make check
make help
```

`make check` lints Markdown, tests repository tooling with a minimum 90% line coverage, and runs Rust formatting, Clippy, unit tests, native integration tests, queue model checking and 80% native, Python and Node product line-coverage gates. `make scan` verifies the remote new-code policy and scans a clean Git commit when `SONAR_TOKEN` is available. See [quality setup](docs/quality.md) for the current analysis scope and local Rust coverage workflow.

## Contributing

Design feedback and implementation proposals are welcome through [issues](https://github.com/quuxio/otelc/issues) and [discussions](https://github.com/quuxio/otelc/discussions). Read [CONTRIBUTING.md](CONTRIBUTING.md) and the [milestones](docs/roadmap.md) before starting a change. Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).

## License

Released under [AGPL-3.0](LICENSE). No separate runtime linking exception is granted by this repository.

## Local metrics, lifetimes and benchmarks

Start with the [developer 101](docs/developer-101.md) for source examples, configuration, annotations and live metrics controls.

| Task | Command |
| --- | --- |
| [Start the Collector, Prometheus and Grafana](docs/observability-stack.md) | `make stack-up` |
| [Build the internal C/C++ examples](docs/local-implementation.md) | `make examples` |
| [Compare plain, probes-disabled and metrics-enabled builds](docs/benchmarks.md) | `make benchmark` |
| [Measure metrics off/on in the same process](docs/benchmarks.md#live-metrics-latency-in-one-process) | `make benchmark-live` |
| [Verify stored traces for every language](docs/traces.md) | `make trace-check` |

Open the [Grafana dashboard](http://localhost:3000/d/otelc-local) after starting the stack. For exception-enabled C++, select `examples/exceptions.toml` with the matched LLVM backend. Benchmark reports retain raw samples and telemetry-loss evidence.

- [Common configuration](docs/common-configuration.md): one schema-2 policy for C, C++, Rust, Python, Java, JavaScript, TypeScript and Go.
- [Language guides](docs/languages.md): adapter setup and current support. See the [roadmap](docs/roadmap.md#todo-language-adapters) for remaining work.
- [Context and lifetime spans](docs/context-and-lifetime-plan.md): the all-language delivery plan, with [Python task and worker context](docs/python-task-context.md) and [Java executor qualification](docs/java-task-context.md).
- [Object lifetimes](docs/object-lifetimes.md): the C++ guard is an opt-in prototype requiring source edits. Automatic lifetime instrumentation without source edits remains planned.
