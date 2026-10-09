# Python collection lifetime spans

Use common configuration to measure selected ordinary Python instances from successful initialisation to observed collection. Application and dependency source stays unchanged. Collection is not a deterministic close/destructor boundary or a leak verdict. Resource close/dispose and arbitrary object lifetime spans remain separate work.

## Run the unchanged example

```sh
make setup
make build
make stack-up
./target/debug/quux-otelc --config examples/python-collection.toml --language python doctor
OTELC_REPORT_PATH=build/python-collection-report.json \
  ./target/debug/quux-otelc --config examples/python-collection.toml python examples/apps/python_collection_app.py
```

The source contains an ordinary class and factory:

```python
class Item:
    def __init__(self, value, cycle=False):
        self.value = value
        if cycle:
            self.cycle = self

def create():
    return [Item(1), Item(2), Item(3, cycle=True)]
```

The application checks its original field layout and results, releases the instances and requests collection of its existing cycle. It prints `collections=3; value=6; layouts=true`. The observer never forces collection. The policy selects `create` for function tracing and selects `Item` independently under `[lifetimes]`, with `enabled=true`, `boundary="collection"` and `include=["examples.apps.python_collection_app.Item"]`. Type names use the ordinary initializer's original relative file path and the most-derived class's qualified name. Source/type exclusions win; function exclusions do not disable selected lifetimes. No annotation or guard field is required.

Expected telemetry is one `create` function span and three standalone `Item lifetime` spans. Each lifetime links to the creating function's trace/span identity; it has no parent span. The creating function finishes without waiting for collection. A lifetime can therefore finish after its linked function tree is exported. Collection durations use a monotonic clock, anchored to an epoch start timestamp, and contain no object addresses or payloads.

## Metrics, limits and shutdown

The Docker Collector accepts both signals on port 4318. Prometheus exposes `otelc_lifetime_admitted_total`, `otelc_lifetime_completed_total`, `otelc_lifetime_live` and `otelc_lifetime_duration_seconds`; the duration metric is a histogram. Labels are `code_type_name` and `otelc_lifetime_boundary`, with the configured service resource labels. Instance tokens are span attributes only. See the [metrics stack](observability-stack.md) for endpoints and [trace viewer](traces.md) for Tempo/Grafana. Search Tempo for `{ span.code.type.name = "examples.apps.python_collection_app.Item" }` to find collection spans; the factory's trace contains its function span rather than the separate lifetime spans.

The provisioned `otelc instrumentation` Grafana dashboard has collection count, live-count, cumulative p95 and loss panels beneath the function/native guard panels. For a custom Grafana Prometheus panel, completed counts use `otelc_lifetime_completed_total{service_name="otelc-python-collection"}`; live instances use `otelc_lifetime_live{service_name="otelc-python-collection"}`. Lifetime p95 uses `histogram_quantile(0.95, sum by (le, code_type_name) (rate(otelc_lifetime_duration_seconds_bucket{service_name="otelc-python-collection"}[5m])))`. A short example may not provide enough samples for a rate; inspect cumulative counts and histogram buckets first.

`runtime.max_live_lifetimes` bounds the weak identity registry. Active initialisers have the separate `runtime.max_active_calls` bound; type metric identities use `runtime.max_functions`. Collection spans share the bounded trace export queue. Capacity, unsupported shapes, stale completions and hook/export failures are explicit diagnostics. Application objects, frames and classes are never retained by the registry; it stores weak references and bounded SDK metadata. Weak references are neither hashed nor compared with application equality.

Admitted/completed/live counts and durations apply to instances admitted while metrics are enabled; that decision remains fixed for the instance across live enable/disable. Span admission follows the enabled trace policy and samples standalone lifetime roots independently using `traces.root_sample_ratio`. Sampling does not change aggregate lifetime counts. A creation link is included when a sampled function context is available.

`report.lifetimes` records admitted, completed, current-live, censored, tracked-instance and active-constructor counts, failed initialisations and losses. Failed initialisation creates no successful lifetime; it is a separate diagnostic, not lost telemetry. Shutdown clears registry references and counts still-live observations as censored/incomplete, with their last live count. It creates no completed span or invented duration. Collection after shutdown does not change that final report.

## Qualified boundaries and trade-offs

The qualified shape is CPython 3.12+ with an ordinary synchronous Python `__init__` with a positional receiver defined on the selected class, the standard `object.__new__`, standard `type` metaclass, weak-reference support and no `__del__` in its hierarchy. Own initialisers with `super()` and recursive delegation admit once; classes with `__slots__` can qualify when they already support weak references. Original signatures, layouts, results and exception identity remain application-owned.

Inherited-only/C/decorated/async/generator initialisers and varargs-only receivers, classes without initialisers, custom `__new__`, metaclasses, finalisers/resurrection and dynamic changes to lifecycle methods are unqualified. Initialiser arguments must retain their original receiver binding. Manual initialisation calls on previously unobserved instances are unqualified; reinitialising an already tracked instance does not create another lifetime. Observed unsupported shapes produce loss diagnostics; completely unobserved constructors cannot supply instance counts. Inspection currently inventories functions rather than proving all class shapes.

Weak-reference introspection is unqualified: adding a weak observer is visible to `weakref.getweakrefs`/`getweakrefcount`. This implementation does not promise invisibility to GC/weak-reference introspection, exact GC timing or arbitrary user finalisation schemes. No finaliser is replaced, no object is resurrected and no layout change is used to add weak-reference support. An absent collection event is an incomplete observation, not proof of a leak.

Weak-reference behaviour follows the [Python weak-reference contract](https://docs.python.org/3/library/weakref.html); the qualified successful-initialisation boundary follows the [Python data model](https://docs.python.org/3/reference/datamodel.html#object.__init__).

Local qualification used CPython 3.12.15 with the locked SDK dependencies. Other interpreter builds, free-threaded interpreters, full debugger/profiler combinations and broader GC behaviour require their own qualification. The fixed collection corpus is a scoped byte-channel/layout/retention observation with a separate span witness; it does not prove hidden object state or production overhead.
