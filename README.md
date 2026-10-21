# otelc

<!-- markdownlint-disable MD013 -->

One configuration for source-free instrumentation across languages, using official OpenTelemetry tools wherever possible. Metric collection and emission switch independently inside the running application. Developers do not add imports, annotations, probes or bootstrap code.

This rebuild starts with **Java**. TypeScript/JavaScript, Python, Go, the LLVM backend, C, C++ and Rust follow in that order. The shared policy lists all targets; that does not mean each adapter is implemented. The [previous implementation](https://github.com/quuxio/otelc/tree/archive/pre-rebuild-2026-10-21) is retained for reuse and comparison. See the [rebuild plan and acceptance criteria](docs/rebuild-plan.md).

## Java example and Grafana

Requirements: Python 3.12+, a full JDK 21+, Maven, Node/npm for Markdown checks, and Docker Compose. Set `JAVA_HOME` for the selected JDK. The new stack uses separate ports and volumes, preserving any older otelc stack.

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

Use the [prepared Java launcher](docs/prepared-java.md) for an existing compiled application or generated test main. An explicit classpath/main/source-binding manifest drives the same policy without compiling or editing application inputs. Plain and instrumented commands retain launch identities, separate JVM class-origin evidence and the same live controls.

Prepared launches keep agent diagnostics in `agent.log`. For exact application stderr comparisons, select `--diagnostic-profile` in both lanes; it disables JVM CDS consistently and retains all application output without filtering. Use the default profile for ordinary JVM/CDS performance measurements and keep the profile label with results.

## Validation and limits

```sh
make check
```

Checks include policy validation, source/checksum preservation, decoded OTLP, exceptions, all four same-process metric states, cached instruments, trace continuity, lifetime observation, complete source exclusion and coverage. An empty resolved lifetime selection reports zero capacity loss; an expected observer that cannot be read still reports unknown and prevents benchmark publication. Java requires the exact pinned official agent and matching SDK extension API. Dynamic meter configuration currently uses a pinned internal SDK bridge; incompatible upgrades must fail validation.

The launcher supports the internal Java example and explicit prepared classpaths. Automatic build-system discovery, arbitrary classloaders/inheritance/delegating constructors, more precise overloaded-method selection, other languages and further platform qualification remain work. Stock Java selection is used only when all selected overloads can be honoured; partial overload exclusions fail before launch. The stack is local, with anonymous Grafana viewing bound to loopback. Stop its containers with `make stack-down`; data volumes remain.
