# Python resource lifetime spans

Select an existing resource class and release method externally. Application and dependency source stays unchanged: no decorator, guard, import or additional object field is required. This boundary measures successful ordinary initialisation to the first normal return from the configured release method. It does not guess that every method named `close` releases a resource.

## Run the unchanged example

```sh
make setup
make build
make stack-up
python3 examples/apps/python_resource_app.py
./target/debug/quux-otelc --config examples/python-resource.toml --language python doctor
OTELC_REPORT_PATH=build/python-resource-report.json \
  ./target/debug/quux-otelc --config examples/python-resource.toml python examples/apps/python_resource_app.py
```

Both ordinary and instrumented runs print `resources=2; value=7; failed_close=1; layouts=true; retained=0`. The source has an ordinary `Resource.__init__`, a `close(fail=False)` method and a factory returning two resources. A failed close raises its original exception; a subsequent normal close completes that resource. Repeated close remains application-owned and creates no second lifetime span. The example verifies the original instance fields before and after release. It also uses its own weak references and collection requests: a strong-reference control must remain alive, and both closed resources must collect after application owners are cleared while the observer is still running. The instrumentation never requests collection.

The common policy adds explicit bindings alongside independent lifetime selection:

```toml
[lifetimes]
enabled = true
boundary = "resource"
include = ["examples.apps.python_resource_app.Resource"]

[[lifetimes.resources]]
type_name = "examples.apps.python_resource_app.Resource"
release = "close"
completion = "normal_return"
```

Bindings use exact source-qualified type names, with no wildcards, and ordinary ASCII method identifiers. At most 128 unique bindings are allowed. `completion` defaults to `normal_return`; other completion rules are rejected. A false return still completes the observation. Configure this rule only for an API where normal return is an appropriate success boundary. A method that physically releases something and then raises remains an incomplete observation under this contract.

## Telemetry and failure cases

Expected example telemetry is one `create` function span and two standalone `Resource lifetime` spans, each linked to its creation context and carrying `otelc.lifetime.boundary=resource`. Creation functions finish normally without waiting for release. See the [Collector/Grafana stack](observability-stack.md) and [Tempo trace viewer](traces.md). Search Tempo for `{ resource.service.name = "otelc-python-resource" }`.

The shared `otelc_lifetime_admitted_total`, `otelc_lifetime_completed_total`, `otelc_lifetime_live` and `otelc_lifetime_duration_seconds` metrics carry type/boundary labels. The example admits/completes two resources and leaves zero live observations. `otelc_lifetime_failed_releases_total` and `report.lifetimes.failed_releases` count the failed close independently of telemetry loss. The provisioned dashboard adds resource completed/live/p95 and failed-release panels below the collection panels. Query `otelc_lifetime_completed_total{service_name="otelc-python-resource"}` to isolate this example.

An exception leaves the observation open for a later successful release. Nested recursive calls wait for their outermost release call to return normally. Independent concurrent calls can complete the resource once; another normal return does not duplicate completion. No lock is held while application release code runs. An observed release that exceeds the frame limit produces `release_capacity` loss rather than an invented completion.

Collection without an observed release censors the resource, records `unreleased_collection` and emits no completed span or duration. The final live count includes censored observations whose release could not be established; it is not a leak verdict or proof that an external resource is still open. Shutdown similarly censors remaining instances, with explicit incomplete-constructor/release diagnostics. The observer never invokes close itself, forces collection or retains application objects/frames. Tokens and weak references use bounded metadata under `runtime.max_live_lifetimes` and `runtime.max_active_calls`.

## Qualified scope

The constructor restrictions and weak-reference/introspection trade-offs in the [collection guide](python-collection.md#qualified-boundaries-and-trade-offs) also apply. Release must be an ordinary synchronous Python method defined on the selected class with a positional receiver. Its declared code name must match the configured release name; aliases such as `close = finish` are unqualified when bound as `close`. The receiver binding must remain unchanged through return. If rebinding is detected at return it records `receiver_changed` and leaves the observation incomplete; earlier collection can instead censor it as an unreleased resource. Async/generator methods, descriptors, static/class methods, inherited-only/C release methods and dynamic lifecycle replacement are unqualified. Constructor/destructor names cannot be configured as release methods. Unsupported observed shapes produce diagnostics without changing the application method.

Object destruction, arbitrary allocation/acquisition APIs, external/process-transferred ownership and automatic context-manager interpretation remain separate work. Existing `__exit__` can be bound explicitly when its normal return meets this contract; the observer does not infer exception suppression or close status. Other languages carry these common bindings but still reject unsupported lifetime execution. Metrics admission remains fixed at initialisation across live enable/disable; standalone span sampling does not change aggregate resource counts.

The example and tests establish these boundaries and telemetry observations. Resource-specific production overhead and broader runtime/interpreter combinations require separate qualification; function benchmark results do not establish resource tracing overhead.
