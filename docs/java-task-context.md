# Java executor task context

Enable `propagation.tasks=true` with `traces.enabled=true` in the common schema-2 policy. The agent captures the selected submitting method's trace identity at standard `ThreadPoolExecutor.execute`, reached by ordinary pool `submit` calls. In-memory JDK probes restore it while the original `FutureTask` runs. Application source, class files, queue entries and Future objects stay unchanged.

## Example and commands

The [ordinary worker application](../examples/apps/JavaWorkerApp.java) contains no otelc imports or annotations:

```java
static int child(int value) {
  if (value < 0) throw ORIGINAL;
  return value + 1;
}
static FutureTask<Integer> root(ThreadPoolExecutor pool, int value) {
  submitted = new FutureTask<>(() -> child(value));
  pool.execute(submitted);
  return submitted;
}
```

The [external policy](../examples/java-worker-context.toml) selects `root` and `child` independently of `main`:

```toml
[traces]
enabled = true
[propagation]
tasks = true
```

Build the agent and launcher, then compare matching JVM settings:

```sh
make build java-build
make stack-up
java -Xshare:off examples/apps/JavaWorkerApp.java
OTELC_REPORT_PATH=/tmp/java-workers.json ./target/debug/quux-otelc   --config examples/java-worker-context.toml java examples/apps/JavaWorkerApp.java
```

Both print `results=11,21; original-error=true; cancelled=true; future-identity=true; rejection-identity=true`. Full sampling produces eight spans across five trees: five submitting roots and three worker children. Two spans report escaping errors, preserving the original worker exception and rejection instances. A cancelled queued task executes no child. A late child can start after its parent ends without extending the parent's duration.

The task launcher disables JVM class-data sharing with `-Xshare:off`. Its minimal bootstrap dispatch bridge otherwise causes an extra JVM CDS warning. Use the same flag in the plain benchmark lane; startup/CDS comparisons are a separate qualification. The SDK stays in the isolated agent namespace. The VM must allow retransformation of `FutureTask` and `ThreadPoolExecutor`; launch fails explicitly if it cannot install both probes.

## Boundaries and failure behaviour

Qualified execution uses standard JDK platform-thread pools and ordinary `FutureTask` submissions. Completion, cancellation before execution, pre-cancelled tasks and rejected submissions release reservations. Cancelling a running FutureTask retains its reservation until every original run scope exits, including a callable that continues after cancellation or ignores interruption. Worker scopes restore previous private context on return or failure. Original results, Future identity and queued objects remain intact. Weak identity keys and immutable trace metadata retain no application task, callable or payload.

`runtime.max_active_calls` bounds the task registry and continuation leases separately from active method frames. The runtime reports `traces.pending_contexts`. Capacity loss invalidates the affected whole tree. After registry overflow or ambiguous reuse of one pending FutureTask by different parents, untracked FutureTask execution remains conservatively suppressed until process restart; selected suppressed work reports `context_untracked`. Newly captured submissions can recover when capacity becomes available. This bounded fallback prevents a lost continuation from being misleadingly resampled as a new root. Metrics still count executed selected methods.

Uncollected pending work is incomplete at shutdown. Collection of an abandoned FutureTask reports `context_incomplete`, invalidates its tree and releases its lease. The observer never forces collection. Probe dispatch failures report `context_hook` where possible and cannot replace application failures.

Bare Runnable execution, custom FutureTask subclasses, scheduled/virtual-thread executors, direct threads and CompletableFuture continuations remain unqualified. Requests through unsupported captured task classes invalidate their parent tree with `context_unsupported`; that diagnostic is not a claim to propagate arbitrary custom schedulers. Discard rejection policies and `shutdownNow` returning unexecuted tasks can leave pending observations censored until collection or shutdown. Keep propagation disabled for those execution models until separately qualified. Existing application SDK context, HTTP propagation and automatic object/resource lifetime spans are separate work.

## Qualification

The focused suite checks reused workers, late children, cancellation, pre-cancelled tasks, duplicate completion, exception identity, capacity suppression, weak retention, sampling and shutdown. Real agent subprocesses compare unchanged application bytes and process output and decode causal OTLP parents. `make java-check` enforces the minimum 80% Java gate; `make check` also covers shared policy and launcher behaviour. oteleq qualification is supplied by its separately maintained workload observer before the delivery is marked complete.
