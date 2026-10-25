# otelc

<!-- markdownlint-disable MD013 -->

One configuration for source-free instrumentation across languages, using official OpenTelemetry tools wherever possible. Metric collection and emission switch independently inside the running application. Developers do not add imports, annotations, probes or bootstrap code.

This rebuild starts with **Java**. TypeScript/JavaScript, Python, Go, the LLVM backend, C, C++ and Rust follow in that order. The shared policy lists all targets; that does not mean each adapter is implemented. The [previous implementation](https://github.com/quuxio/otelc/tree/archive/pre-rebuild-2026-10-21) is retained for reuse and comparison. See the [rebuild plan and acceptance criteria](docs/rebuild-plan.md).

The [Node source candidate](docs/node-source.md) now provides public launches for ordinary JavaScript and both Microsoft TypeScript compiler profiles, with retained [launch input evidence](docs/node-launch-inputs.md). Its function/context/lifetime probes and live controls are being qualified. [Paired benchmarks](docs/benchmarks.md) compare it with Java and retain separate application/SDK resource observations. Complete prepared provenance and final independent language acceptance remain open. `plan` reports these profiles separately from delivered adapters.

The [development notes](docs/node-development.md) describe compiler/runtime checks, independent SDK drain, qualified source boundaries and remaining delivery gaps.

The merged [Node SDK/control foundation](docs/node-sdk-controls.md) supplies official SDK/exporters, independent live metric gates and the shared control client. Source probes and application launching belong to the separate Node candidate.

The [Python frontend candidate](docs/python-native.md) launches original CPython sources with selected function metrics/spans, weak object/resource lifetime observations, official thread propagation and live controls. The [SDK metric gates](docs/python-sdk-controls.md) and [process bridge](docs/python-sdk-process.md) isolate aggregation/export and retain checked drain evidence. Its [paired benchmarks](docs/benchmarks.md) compare a direct stock CPython process with the prepared instrumented application, retaining body latency, separate launch-to-first-response timing and actual per-process CPU/RSS. The [HTTP candidate](docs/python-http.md) also drives official urllib/WSGI tooling with the same policy and live metric controls. Broader lifetime/framework shapes, full source evidence and language qualification remain work. Python is still reported as remaining by `plan`. The [prepared native launcher](docs/python-prepared-launch.md) connects the [entry input component](docs/python-input-journal.md) and [fixed request/argv layer](docs/python-launch-recipe.md), retaining actual ownership and post-drain input checks. Ordinary source/whole-bytecode loader reads are retained in the native journal. Selected function starts carry native code descriptors and span/metric code IDs. The post-run reader independently checks descriptor framing, types, graph bounds, aliases and filename bytes using the application's recorded filesystem codec, without constructing or executing code. The common [health command](docs/python-health.md) exposes live and retained Python application/SDK counters without changing metric switches or draining lifetime events. A [post-run comparison component](docs/python-code-comparison.md) compares retained source/pyc inputs in isolated workers, with explicit ambiguous, unmatched and incomplete results. Automatic publication, source-origin proof, whole-program coverage, dependency closure and provider admission remain outstanding.

`plan` lists Python as a candidate and keeps it in `remaining`; it is not reported as implemented. Tooling consumers can inspect `otelc.prepared_python.protocols()` before launch to negotiate Python formats. These discovery responses do not establish source origin or provider qualification; see the [prepared launcher guide](docs/python-prepared-launch.md#negotiate-formats-without-launching).

The [native input component](docs/python-input-journal.md#validation-scope) includes an SDK-independent descriptor encoder for source-origin work. The [origin-journal component](docs/python-origin-journal.md) defines versioned read/root/execution relations. Its explicit [native source-entry producer](docs/python-native-entry.md) observes checked compiler roots and weak code descendants for its declared image profile. The separate optional [source-loader component](docs/python-loader-origins.md) connects actual source reads, compiler returns and loader returns, with an optional application-root boundary. The optional [SDK join component](docs/python-origin-sdk-joins.md) binds authenticated selected starts to the same weak catalogue. The explicit [public origin profile](docs/python-prepared-origins.md) connects these observers in both prepared lanes. Custom-loader origins, broader closure and provider qualification remain work. Ordinary existing source-loader caches have a separate explicit profile below. Default launches still negotiate journal 1; every profile reports `source_complete=false`.

The explicit [cached-loader origin candidate](docs/python-cached-origins.md) adds `--profile python-native-cache-origins-v3` to public CLI/prepared execution. It observes ordinary existing source-loader caches, uses the same common policy and live controls, and retains distinct journal-3 deserialisation evidence in both lanes. The shared cached-import example exercises the original workload, errors and lifetime boundaries without instrumentation code. Independent provider qualification and final delivery remain work; the source-only profile stays journal 2.

Native launch also retains the selected interpreter's pre-site executable identities, preserving the tested Homebrew alias and copied-venv metadata under ordinary and `-S` startup.

The [native input observer](docs/python-input-journal.md#evidence-and-failure-boundary) also retains ordinary raw `FileIO` loader reads, including older CPython sourceless imports. Its tests compare original output, errors, file lifetimes, nested/concurrent reads and profiling metadata. This remains input evidence; compiler-origin and complete-source qualification are separate work.

The [Go SDK controls component](docs/go-sdk-controls.md) adds independent live gates around an isolated official Go metric pipeline. Source-free Go weaving, public launch/control integration, context/lifetime probes and qualification remain work; this component does not promote Go to a delivered adapter.

The [LLVM compiler component](docs/llvm-backend.md) is a candidate for original C/C++ compilation and a separate ordinary IR example. It preserves tested exception cleanup and applies common function selection. Its explicit registered profile supplies selected function addresses and UTF-8 names before ordinary application initialisers. The [native function SDK engine](docs/native-functions.md) now connects compiler-inserted calls in unedited C/C++ fixtures to actual SDK spans and call/error/duration metrics, with SDK context restoration and independent metric gates. The native launch candidate below adds generated bootstrap and OTLP export; task/service propagation and automatic lifetime instrumentation remain work.

The [native SDK control component](docs/native-sdk-controls.md) reuses official C++ SDK meter configuration and filtering for live collection, with a separate emission delegate. Its [native analysis pipeline](docs/native-analysis.md) retains actual compiler context and exact-source coverage for Sonar. Complete native language qualification and C resource support remain work. The [C++ lifetime candidate](docs/native-public-lifetimes.md) connects supported construction/destruction to public build and launch.

The explicit [native launch candidate](docs/native-launch.md) connects original C/C++ compilation to official SDK OTLP/HTTP export, live metric controls and retained health. It uses source-qualified function identities and a generated startup object, preserving developer source. The separate owned build/launch candidate adds explicit build contexts and [C++ object lifetimes](docs/native-public-lifetimes.md). Native task/service propagation, automatic build discovery and final independent language qualification remain work.

The [native prepared-launch candidate](docs/native-prepared.md) exposes versioned build, prepare, command, release and execution records. Its pre-exec gate holds an owned child before application or loader-library constructors run in both lanes, preserving its identity, literal arguments and streams across release. Independent oteleq integration and final language qualification remain work.

The [C++ lifetime semantics component](docs/native-lifetimes.md) uses official Clang AST/type/USR APIs to inventory construction and variable materialisation without source edits. The [object probe component](docs/native-object-probes.md) connects selected non-trivial construction/destruction to actual SDK lifetime spans and metrics in unchanged internal fixtures. The [public lifetime candidate](docs/native-public-lifetimes.md) connects those probes and the [launcher SDK bridge](docs/native-object-sdk.md) to common-policy build/preparation, actual OTLP export, live gates and object health. Broader boundaries, performance/dashboard evidence and independent language qualification remain work.

The explicit [native build-3 context](docs/native-build-context.md) adds per-unit compiler flags/directories, private VFS inputs, main-symbol rename and an uninstrumented observation driver. It reuses the common policy and owned launch controls. Automatic build discovery and generated-provider qualification remain work.

## Java example and Grafana

Requirements: Python 3.12+, a full JDK 21+, Maven, Node 24.11+/npm for SDK and Markdown checks, and Docker Compose. Set `JAVA_HOME` for the selected JDK. The new stack uses separate ports and volumes, preserving any older otelc stack.

```sh
make setup java-build
make stack-up
bin/quux-otelc plan
bin/quux-otelc example java
```

Use `bin/quux-otelc` from the checkout after `make setup`. It starts the tooling host in isolated/no-site mode before Python can run installed startup hooks. `OTELC_PYTHON=/path/to/sdk-venv/bin/python bin/quux-otelc ...` selects another SDK environment with the locked wheels installed. The old `python -m otelc` entry point is refused; application startup hooks still run normally in the application. See the [prepared Python launch guide](docs/python-prepared-launch.md).

Enter `10` to run ten operations, `gc` to request collection from the test application, or `quit` to exit. The ten-operation checksum is `41284864`. The [ordinary application](examples/java/BenchmarkApp.java) has no telemetry dependency. otelc generates official-agent configuration and loads its own small extension; it does not modify application source or classes on disk.

Run the paired benchmark and open [Grafana comparison](http://localhost:3001/d/otelc-comparison) or [function metrics and object lifetimes](http://localhost:3001/d/otelc-lifetimes):

```sh
make benchmark
```

The benchmark uses [one policy](examples/otelc.toml), validates checksums, retains plain and four live-state samples, and exports driver measurements with the official Python SDK. Java also retains actual JVM CPU/RSS windows, including its embedded SDK, using the shared report format. Raw reports are under `build/benchmarks/`. The [benchmark guide](docs/benchmarks.md) shows how to group Java, JavaScript, Microsoft TypeScript and Python runs under one Grafana comparison ID. Other languages follow as their adapters are implemented; missing languages are not fabricated.

## Collection, emission and lifetimes

The new policy is schema 3: it separates `metrics.collection` from `metrics.emission` and replaces the archived schemas. Generated tool configuration is not user-maintained. Both live switches are installed at initial launch, including when the policy starts off. Traces keep their launch-time configuration.

```toml
[metrics]
collection = true
emission = true
```

The benchmark changes these states in one instrumented PID. For manual control, the generated private runtime directory contains `control.sock`:

```sh
bin/quux-otelc metrics --socket /tmp/otelc-RUNTIME/control.sock status
bin/quux-otelc metrics --socket /tmp/otelc-RUNTIME/control.sock off
bin/quux-otelc metrics --socket /tmp/otelc-RUNTIME/control.sock collection on
bin/quux-otelc metrics --socket /tmp/otelc-RUNTIME/control.sock emission on
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

[Runtime health](docs/runtime-health.md) records PID/policy/selection identity, raw SDK exporter activity and bounded final flush/shutdown outcomes in `health.json`, including while metrics are off. Inspect it with `bin/quux-otelc health --socket ...` or `--report ...`. Queue loss stays unavailable until a supported clean drain establishes it; exporter success still requires separate downstream receipt evidence.

[Invocation attribution](docs/action-witness.md) is an explicit diagnostic profile for synchronous Java actions on platform threads. Frozen original-method bindings, actual loaded origins and role probes tie an action to its root, target and observed child spans. A separate private protocol records readiness, arm/consume, exits and export outcomes; decoded downstream receipt is still required. Default launches and the existing metrics/health protocols are unchanged. This profile does not establish arbitrary concurrent, async or cross-service attribution.

The separate [private-handle compiler component](docs/java-handle-witness.md) binds generated private-method caller shapes without loading application classes. Its explicit runtime candidate adds actual operand/receiver checks and a separate optional diagnostic launch profile. Generated consumer integration and independent admission remain work; default launches continue to refuse its bindings.

## Validation and limits

```sh
make check
```

The full check also requires LLVM 22, CMake, Ninja, Protobuf with its compiler, and libcurl development headers/libraries. CI installs these dependencies. It builds and exercises the native HTTP runtime, rejects skipped launcher tests and enforces the component coverage floors; see [native launch checks](docs/native-launch.md#focused-checks).

Checks include policy validation, source/checksum preservation, decoded OTLP, exceptions, all four same-process metric states, cached instruments, trace continuity, lifetime observation, complete source exclusion and coverage. An empty resolved lifetime selection reports zero capacity loss; an expected observer that cannot be read still reports unknown and prevents benchmark publication. Java requires the pinned agent, extension SPI and SDK combination qualified by the integration checks. Dynamic meter configuration currently uses a pinned internal SDK bridge; incompatible upgrades must fail validation.

The launcher supports the internal Java example and explicit prepared classpaths. Automatic build-system discovery, arbitrary classloaders/inheritance/delegating constructors, more precise overloaded-method selection, other languages and further platform qualification remain work. Name-based Java selection is used only when all selected overloads can be honoured; partial overload exclusions fail before launch. The stack is local, with anonymous Grafana viewing bound to loopback. Stop its containers with `make stack-down`; data volumes remain.

Function probes use the official agent and instrumenter with exception-safe hooks. ERROR spans contain the original thrown type, with no message, stack trace or cause unwrapping: those operations can execute application getters or formatters. The original throwable and application future remain unchanged. Official asynchronous completion support keeps selected future operations open until completion; metric switches remain independent. See [Java exception safety](docs/java-exceptions.md).

Selected functions require readable return-type metadata for the pinned advice implementation. Preflight rejects unavailable selected return types before application execution. Missing types on unrelated declarations or unused parameters remain accepted. See the [prepared launcher](docs/prepared-java.md) for this boundary.

The build includes the official upstream trace-state parser fix in a verified bootstrap JAR, applied automatically by instrumented launches. The official agent remains unmodified. See [Java dependency versions and security backport](docs/java-dependencies.md) for the exact compile/runtime combination, source attribution and validation boundary.
