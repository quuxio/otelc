# otelc

<!-- markdownlint-disable MD013 -->

One configuration for source-free instrumentation across languages, using official OpenTelemetry tools wherever possible. Metric collection and emission switch independently inside the running application. Developers do not add imports, annotations, probes or bootstrap code.

This rebuild starts with **Java**. TypeScript/JavaScript, Python, Go, the LLVM backend, C, C++ and Rust follow in that order. The shared policy lists all targets; that does not mean each adapter is implemented. The [previous implementation](https://github.com/quuxio/otelc/tree/archive/pre-rebuild-2026-10-21) is retained for reuse and comparison. See the [rebuild plan and acceptance criteria](docs/rebuild-plan.md).

The [JavaScript/Microsoft TypeScript development notes](docs/node-development.md) describe the current local adapter, compiler/runtime checks, independent SDK drain and remaining delivery gaps. This work does not yet enable another delivered language in the public CLI.

The [Node SDK/control building block](docs/node-sdk-controls.md) is under construction for TypeScript/JavaScript: official SDK/exporters, independent live metric gates and the shared control client. It does not yet enable source-free JS/TS application launching, lifetimes or benchmarks.

## Java example and Grafana

Requirements: Python 3.12+, a full JDK 21+, Maven, Node 24.11+/npm for SDK and Markdown checks, and Docker Compose. Set `JAVA_HOME` for the selected JDK. The new stack uses separate ports and volumes, preserving any older otelc stack.

```sh
make setup java-build
make stack-up
.venv/bin/python -m otelc plan
.venv/bin/python -m otelc example java
```

Enter `10` to run ten operations, `gc` to request collection from the test application, or `quit` to exit. The ten-operation checksum is `41284864`. The [ordinary application](examples/java/BenchmarkApp.java) has no telemetry dependency. otelc generates official-agent configuration and loads its own small extension; it does not modify application source or classes on disk.

Run the paired benchmark and open [Grafana comparison](http://localhost:3001/d/otelc-comparison) or [function metrics and object lifetimes](http://localhost:3001/d/otelc-lifetimes):

```sh
make benchmark
```

The benchmark uses [one policy](examples/otelc.toml), validates checksums, retains plain and four live-state samples, and exports driver measurements with the official Python SDK. Raw reports are under `build/benchmarks/`. Select one comparison run in Grafana. Later language examples will use this same workload and policy so Java can be compared with C++, Rust, C, Python, JavaScript, Go and Microsoft TypeScript. Only delivered adapters produce data; missing languages are not fabricated.

## Collection, emission and lifetimes

The new policy is schema 3: it separates `metrics.collection` from `metrics.emission` and replaces the archived schemas. Generated tool configuration is not user-maintained. Both live switches are installed at initial launch, including when the policy starts off. Traces keep their launch-time configuration.

```toml
[metrics]
collection = true
emission = true
```

The benchmark changes these states in one instrumented PID. For manual control, the generated private runtime directory contains `control.sock`:

```sh
.venv/bin/python -m otelc metrics --socket /tmp/otelc-RUNTIME/control.sock status
.venv/bin/python -m otelc metrics --socket /tmp/otelc-RUNTIME/control.sock off
.venv/bin/python -m otelc metrics --socket /tmp/otelc-RUNTIME/control.sock collection on
.venv/bin/python -m otelc metrics --socket /tmp/otelc-RUNTIME/control.sock emission on
```

Off/on controls both together; the separate controls change only their named state. Emission-off starts no new exports. Status reports in-flight requests that must settle before a strict off barrier is effective. Collection-off suppresses cached SDK instruments, observable callbacks and collection from disabled meters; emission-on alone does not make those meters emit a stale snapshot. Cumulative values remain available after collection is re-enabled; emission-only suppression can include the silent interval in a later cumulative point. Installed hooks, enabled tracing and lifecycle bookkeeping can retain overhead while metrics are off.

Java observes selected final classes with one constructor. It measures successful construction to **observed GC** and, when an existing synchronous zero-argument void release method matches, construction to **resource release**. Failed or duplicate releases do not invent completion. The observer uses bounded weak identity metadata, does not call application `hashCode`/`equals`, and never requests production GC. The explicit `gc` command is part of the test fixture. Capacity losses invalidate benchmark publication. These boundaries differ from C++ destruction and Rust drop; dashboards keep their labels separate.

See [measuring object and resource lifetimes](docs/lifetimes.md) for configuration, metrics, spans, failure cases and the language delivery plan.

[Java task and HTTP propagation](docs/java-context.md) uses common `[context]` switches to drive the official agent. Ordinary task/client/server examples, source-free launch commands and tested parentage/sampling/preservation boundaries are included. These switches are selected at launch; metric collection/emission still change live without affecting traces.

Open the [task and service trace dashboard](http://localhost:3001/d/otelc-traces) to search by service and inspect a selected span tree. It works with the local anonymous Viewer account and does not require Grafana Explore. The dashboard reuses the archived trace view with the rebuild's Tempo datasource and example service.

Use the [prepared Java launcher](docs/prepared-java.md) for an existing compiled application or generated test main. An explicit classpath/main/source-binding manifest drives the same policy without compiling or editing application inputs. Plain and instrumented commands retain launch identities, separate JVM class-origin evidence and the same live controls.

Prepared JAR launches resolve ordinary and multi-release archives through the selected JDK. Launch schema 2 records the actual member names and bytes used for metadata/helper inspection; directory-only launches retain schema 1. Original application archives, resources and runtime classpaths stay unchanged. Consumers must explicitly support the new schema.

Application-owned helper names are preserved. When names conflict, the launcher generates a private extension with renamed helpers and records its identity; developers do not rename application classes.

Prepared launches keep agent diagnostics in `agent.log`. For exact application stderr comparisons, select `--diagnostic-profile` in both lanes; it disables JVM CDS consistently and retains all application output without filtering. Use the default profile for ordinary JVM/CDS performance measurements and keep the profile label with results.

[Runtime health](docs/runtime-health.md) records PID/policy/selection identity, raw SDK exporter activity and bounded final flush/shutdown outcomes in `health.json`, including while metrics are off. Inspect it with `python -m otelc health --socket ...` or `--report ...`. Queue loss stays unavailable until a supported clean drain establishes it; exporter success still requires separate downstream receipt evidence.

[Invocation attribution](docs/action-witness.md) is being implemented separately. The offline Java binding compiler identifies exact compiled envelope/rendezvous/wrapper/target methods and refuses unsafe diagnostic harness shapes. Read-only class capture is tested against the real agent before SDK advice and against actual loaded identities/origins. Runtime scoping and per-action export evidence are still pending; these building blocks do not enable a witness capability.

## Validation and limits

```sh
make check
```

Checks include policy validation, source/checksum preservation, decoded OTLP, exceptions, all four same-process metric states, cached instruments, trace continuity, lifetime observation, complete source exclusion and coverage. An empty resolved lifetime selection reports zero capacity loss; an expected observer that cannot be read still reports unknown and prevents benchmark publication. Java requires the pinned agent, extension SPI and SDK combination qualified by the integration checks. Dynamic meter configuration currently uses a pinned internal SDK bridge; incompatible upgrades must fail validation.

The launcher supports the internal Java example and explicit prepared classpaths. Automatic build-system discovery, arbitrary classloaders/inheritance/delegating constructors, more precise overloaded-method selection, other languages and further platform qualification remain work. Name-based Java selection is used only when all selected overloads can be honoured; partial overload exclusions fail before launch. The stack is local, with anonymous Grafana viewing bound to loopback. Stop its containers with `make stack-down`; data volumes remain.

Function probes use the official agent and instrumenter with exception-safe hooks. ERROR spans contain the original thrown type, with no message, stack trace or cause unwrapping: those operations can execute application getters or formatters. The original throwable and application future remain unchanged. Official asynchronous completion support keeps selected future operations open until completion; metric switches remain independent. See [Java exception safety](docs/java-exceptions.md).

Selected functions require readable return-type metadata for the pinned advice implementation. Preflight rejects unavailable selected return types before application execution. Missing types on unrelated declarations or unused parameters remain accepted. See the [prepared launcher](docs/prepared-java.md) for this boundary.

The build includes the official upstream trace-state parser fix in a verified bootstrap JAR, applied automatically by instrumented launches. The official agent remains unmodified. See [Java dependency versions and security backport](docs/java-dependencies.md) for the exact compile/runtime combination, source attribution and validation boundary.
