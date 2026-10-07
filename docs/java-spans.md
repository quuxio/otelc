# Java method spans

The external JDK 21+ agent can produce sampled method-body spans under the common schema-2 configuration. Application source, compiled classes and JARs on disk stay unchanged. The agent uses its private, shaded OpenTelemetry SDK rather than replacing an application's global tracer provider.

## Run and view

Start the [Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build java-check
docker compose up -d
java examples/apps/JavaTraceApp.java
./target/debug/quux-otelc --config examples/java-traces.toml --language java doctor
OTELC_REPORT_PATH=/tmp/java-traces.json \
  ./target/debug/quux-otelc --config examples/java-traces.toml \
  java examples/apps/JavaTraceApp.java
```

Both runs print `trace results preserved`. The [unedited example](../examples/apps/JavaTraceApp.java) contains recursive methods, a synchronous parent/child call, virtual-thread work, preserved exception objects, and successful/failed constructors. It records ten invocations in six trace trees.

```java
static int recursive(int depth) {
  return depth == 0 ? 0 : 1 + recursive(depth - 1);
}
static int child() { return 42; }
static int parent() { return child(); }
```

The [external policy](../examples/java-traces.toml) enables traces and selects methods. Existing optional bytecode annotations and exclusions still apply as described in the [Java 101 guide](java.md). The same agent supports normal classpath/JAR launches. No application OpenTelemetry import or manual span is required.

Open the [read-only trace dashboard](http://localhost:3000/d/otelc-traces), set **Service** to `otelc-java-traces` and select a trace to see its spans. Collector batching and Tempo ingestion take time. A successful export acknowledgement alone does not prove downstream storage.

## Behaviour and boundaries

Spans cover the original method body from probe entry until normal return or an escaping `Throwable`. Arguments, returned objects and exception messages are not recorded or retained by the store. Exceptions keep their original identity; caught exceptions do not mark the enclosing method as failed. Constructor timing starts after its base/delegating constructor returns. Native, abstract, synthetic, bridge and class-initialiser methods retain the agent's existing exclusion rules.

Parenting follows selected method calls on the same platform or virtual thread. Each entry restores its caller's private context on exit. Independent tasks and threads start new roots. Returning a `CompletionStage` ends the method span at the original method return; stage completion, automatic task/distributed propagation, manual SDK parenting and span links are unsupported.

With traces enabled, the function cache admits selected methods when they are invoked, rather than when their classes load. Rejected calls suppress descendant spans until the rejected method exits, preventing descendants from appearing as separate roots.

SDK root sampling is inherited by descendants. Reaching a function, active-call or per-tree span limit invalidates the affected whole tree, including completed siblings. Active-tree and completed-tree queue capacities are bounded. Invalidated payloads are released while already admitted methods finish. Unfinished sampled trees are discarded at shutdown. Active and queued records share a hard maximum of 1,048,576 spans; one additional bounded tree may be in flight. A conservative 16 MiB tree budget is checked before SDK encoding, followed by an encoded-size check. This bounds local export; partial downstream acceptance cannot be rolled back.

Trace endpoint, headers, protocol and timeout use the common independent signal precedence documented in [Rust trace settings](rust-spans.md#independent-metrics-and-trace-settings). Empty trace-specific headers suppress generic headers. The existing metric reader drains completed trace trees after each collection, including unchanged metric snapshots. Transient transport failures receive at most three attempts within one trace-phase deadline; partial, malformed, oversized and permanent rejection is not retried. Exhausted batches are discarded and increase `export_loss`. `otelc.trace.dropped_trees`, export-health metrics and the report's `traces` section expose loss, including without further application calls.

Application shutdown keeps its shared time budget. Check `export_finished`: a timeout may leave a daemon SDK operation unfinished. Live `enable`/`disable` controls metric admission only, with existing calls retaining their original admission; configured traces continue. Tracing adds SDK, buffering and transport overhead. Metrics-only benchmarks do not qualify span overhead, and no production latency threshold is claimed.
