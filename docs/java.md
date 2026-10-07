# Java instrumentation 101

The Java adapter adds timing probes to selected class bytecode when the JDK loads it. Original source files, compiled classes and application JARs stay unchanged. It needs a full JDK 21+ and Maven to build the external agent; application code needs no OpenTelemetry dependency, import or annotation.

## Build and run

```sh
make build java-check
./target/debug/quux-otelc --config examples/java.toml --language java doctor
java examples/apps/JavaApp.java
./target/debug/quux-otelc --config examples/java.toml java examples/apps/JavaApp.java
```

Both executions print `72`. The instrumented example records 11 calls, including recursion, a constructor, an escaping exception, an internally caught error and executor work. The annotated example prints `30` and records two calls:

```sh
./target/debug/quux-otelc --config examples/java.toml java examples/apps/JavaAnnotated.java
```

The wrapper also accepts normal JDK launch arguments: `java -cp build/classes package.Main` or `java -jar application.jar`. Run from the project root. When original `.java` files are present, the agent uses the JDK parser to map package and `SourceFile` metadata to project-relative paths. Class-only/JAR applications can use the logical package/source paths described below. Duplicate package/source filenames require narrower source filters.

Ordinary unchanged code is selected externally:

```java
static int process_order(int value) { return value * 3; }
```

```toml
schema_version = 2
languages = ["java"]
[sources]
include = ["examples/apps/Java*.java"]
[functions]
include = ["examples.apps.JavaApp.process_order(int)"]
[adapters.java]
backend = "agent"
```

Method identities include the qualified class, method and argument types. Constructors use `<init>`; nested classes retain `$`. Inspect original compiled bytecode to see exact names:

```sh
javac -g -d build/java examples/apps/JavaApp.java
./target/debug/quux-otelc --config examples/java.toml --language java inspect build/java/examples/apps/JavaApp.class --json
```

## Optional existing annotations

With `annotations.read_existing = true`, the agent recognises compiled `otelc.Instrument`/`otelc.Exclude` or annotations named `OtelcInstrument`/`OtelcExclude`. Use CLASS or RUNTIME retention; SOURCE-only annotations are absent from bytecode. The example declares its own marker types without SDK imports:

```java
@OtelcInstrument
static int selected(int value) { return value * 3; }

static int configured(int value) { return value + 7; }

@OtelcExclude
static int excluded(int value) { return value - 1; }

@interface OtelcInstrument {}
@interface OtelcExclude {}
```

Annotations are optional. Exclusions always win. `inject_generated = true` adds invisible selection metadata only to transformed bytecode. The generated body is equivalent to the following pattern; the original method signature and source stay intact:

```java
long token = Probes.enter("examples.apps.JavaApp.process_order(int)");
try {
  // Original method body; each return records normal completion.
} catch (Throwable error) {
  Probes.exit(token, true);
  throw error;
}
```

Existing exception handlers run first. Escaping exceptions retain their original object and type. Constructor timing starts after its base/delegating constructor returns, preventing access to an uninitialised object. Native, abstract, synthetic, bridge and class-initialiser methods are excluded.

## Metrics, live controls and benchmarks

Start the [Collector, Prometheus and Grafana stack](observability-stack.md). The Java example service is `otelc-java-example`; it exports the shared call/unwind counters and duration histogram in seconds. Resource attributes, histogram boundaries, function/active-call capacities and export deadlines come from the common policy.

Set `runtime.control_socket` to a socket inside an owner-only directory, then use `status`, `enable` and `disable` as described in the [developer guide](developer-101.md). Controls operate inside the same Java PID. Previously admitted calls finish after disable; new disabled calls do not record.

```sh
make benchmark-language LANGUAGE=java LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/java --iterations 10000 --runs 8"
```

This compares uninstrumented execution, disabled probes and enabled metrics, checks matching results and original source hashes, and requires exact call counts, zero losses and complete export. JIT warm-up and host scheduling influence the measured latency; compare repeated reports on the same machine. `make java-check` enforces at least 80% Java product line coverage.

The pinned Java SDK provides metric aggregation and OTLP protobuf encoding. The bounded HTTP sender permits one in-flight request, rejects redirects, malformed/partial acknowledgements and responses over 64 KiB, and honours OTLP header precedence. Failed cumulative snapshots can be retried at the next export interval. Shutdown uses one overall deadline. The SDK's internal marshaler is version-pinned and covered by decoding tests; it must be requalified when upgrading.

## Boundaries

Normal application class loaders that can access the agent are supported; named modules gain the required read edge. An isolated loader or unresolved stack-frame type produces an explicit `unsupported_class` loss rather than changing application behaviour. Check loss counters before trusting a benchmark. Retransformation and already-instrumented classes are rejected. Agent dependencies are shaded to avoid application dependency collisions.

A method returning a future is timed until it returns the future, not until that future completes. Executor methods selected independently are timed on their execution thread. Automatic object lifetimes, spans and AspectJ are unavailable and rejected by configuration. Forced JVM termination cannot guarantee a final export.

For class-only/JAR applications, source selection falls back to the logical package/`SourceFile` path (for example `example/App.java`), or the outer class name if debug source metadata is absent. Parsed local class declarations recover the physical source path when debug metadata is absent, including secondary classes whose name differs from their filename. Local source exclusions still win. Bootstrap/platform classes are protected from this fallback. Disabled admission checks avoid the observation lock. Normal shutdown serialises completed SDK recording with its final snapshot; if recording cannot drain within the shutdown deadline, the report marks incomplete observations and export failure instead of claiming successful completion.
