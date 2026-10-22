# Java task context propagation

<!-- markdownlint-disable MD013 -->

The common policy enables task propagation. This is a launch-time choice; the installed metric collection and emission switches remain live and independent. Enabling propagation currently requires `traces.enabled=true`. The default is false. Setting a language name in this policy does not implement its adapter: this rebuild currently supports Java.

```toml
[traces]
enabled = true
sample_ratio = 1.0

[context]
tasks = true
http = false
```

The Java translator enables the pinned official agent's `executors` module. All other library instrumentations remain disabled by default. Source/function selections still control our direct probes. Developers add no SDK imports, annotations or bootstrap.

## Run the ordinary example

Use JDK 21 and the [local Collector/Grafana stack](../README.md#java-example-and-grafana). Build the example and its digest-bound prepared manifest:

```sh
make setup java-context-examples
make stack-up
```

The [task application](../examples/java/TaskApp.java) contains ordinary Java code such as:

```java
static int parent(int value) throws Exception {
  return POOL.submit(() -> child(value)).get();
}
```

Launch it with the common [context policy](../examples/context.toml):

```sh
.venv/bin/python -m otelc --config examples/context.toml java \
  --manifest build/examples/context/java/TaskApp.json \
  --runtime /tmp/otelc-tasks-$$
```

Enter `run`, `outside`, `cancel`, `reject`, `error`, then `quit`. The first four results are `8`, `21`, `true`, `true`. The error result reports preserved throwable identity and the number of ordinary exception-formatting observations. With task propagation on, the selected child span belongs to its selected parent; a later unrelated call on the reused worker starts a new trace. Cancelled and rejected work does not execute the selected child. Add `--plain` before the application arguments to run without the agent.

Use the selected runtime path with the [live metric commands](../README.md#collection-emission-and-lifetimes). Task traces continue when metrics turn off. The [Grafana function dashboard](http://localhost:3001/d/otelc-lifetimes) shows the selected function metrics. The local stack's anonymous Viewer cannot open Explore. Retrieve stored trace IDs through Tempo's loopback API:

```sh
curl --fail --get --data-urlencode 'q={resource.service.name="otelc-context-examples"}' \
  http://localhost:3201/api/search
```

Read an individual result with `curl --fail http://localhost:3201/api/traces/TRACE_ID`, replacing `TRACE_ID` with a returned `traceID`. The equivalent-workload benchmark stays on its existing policy; this diagnostic application is not a cross-language timing comparison.

## Preservation and tested boundaries

Real JDK 21 JVM comparisons cover single-platform-worker `submit`/`FutureTask`, reuse, cancellation before execution, failed submission and thrown identity. Decoded OTLP checks exact parent/trace identities, unrelated worker calls starting new traces and no executed cancelled child. All four metric states are exercised in the same instrumented PID, including startup-off. Complete application/tooling input identities are rechecked after execution.

Arbitrary executor wrappers, virtual/structured threads and custom classloaders remain qualification work. HTTP propagation is a separate implementation; `context.http=true` is explicitly refused before application launch. Task propagation does not enable asynchronous per-action witness attribution.
