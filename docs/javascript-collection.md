# JavaScript collection lifetime spans

Selected ordinary JavaScript classes can produce collection lifetime metrics and spans without source edits. This Node loader capability measures **successful constructor completion to observed garbage collection**. It does not measure destruction, explicit resource release or leaks. Finalization callbacks are optional and nondeterministic under the [ECMAScript memory-management contract](https://tc39.es/ecma262/multipage/managing-memory.html#sec-finalization-registry-objects); a process can exit without observing collection.

## Ordinary source and configuration

The [unedited example](../examples/apps/javascript_collection_app.cjs) contains an ordinary class:

```js
class WorkItem {
  constructor(value) { this.value = value; }
  compute() { return this.value * 3; }
}
function make(value) { return new WorkItem(value); }
```

The [common policy](../examples/javascript-collection.toml) selects it externally:

```toml
[lifetimes]
enabled = true
boundary = "collection"
include = ["*.WorkItem"]

[traces]
enabled = true
```

Type identities use the project-relative source path and lexical class/function names, for example `examples.apps.javascript_collection_app.WorkItem`. Type selection is independent of function selection. Exclusions win. `inspect` lists the discovered types and their selection; it does not execute static initialisers or constructors.

Existing class comments can opt in when `annotations.read_existing=true`. The [annotated example](../examples/apps/javascript_collection_annotated.cjs) uses:

```js
// otelc.instrument
class AnnotatedItem {
  constructor(value) { this.value = value; }
  compute() { return this.value * 3; }
}
```

The policy deliberately selects `*.WorkItem`; the existing comment admits `AnnotatedItem`. No annotation is required in the ordinary example. Function exclusions in the policy keep constructor/method timing separate from the selected `make` creator.

```sh
make build node-check
make stack-up
./target/debug/quux-otelc --config examples/javascript-collection.toml --language javascript doctor
./target/debug/quux-otelc --config examples/javascript-collection.toml --language javascript inspect examples/apps/javascript_collection_app.cjs --json
node examples/apps/javascript_collection_app.cjs
OTELC_REPORT_PATH=build/javascript-collection.json ./target/debug/quux-otelc --config examples/javascript-collection.toml node examples/apps/javascript_collection_app.cjs
OTELC_REPORT_PATH=build/javascript-collection-annotated.json ./target/debug/quux-otelc --config examples/javascript-collection.toml node examples/apps/javascript_collection_annotated.cjs
```

Both apps print `48`, with two admitted instances. They keep their objects until process exit, so the normal demonstration reports censored observations rather than promising two collection spans. The qualification tests separately collect cycles with an explicit test-only GC driver, compare unchanged source/output against an ordinary launch, preserve the application's own finalizer, decode the real SDK spans and check the creation link. Production instrumentation never calls GC or changes when the application releases references.

## Observation and bounds

Successful construction registers only weak identity metadata. The loader adds an internal static registration block and constructor-body probes in memory; it adds no instance field, prototype member, wrapper class, destructor or finalizer replacement. Default constructors and named/anonymous class expressions retain their class identity and inferred name. A live object does not keep its creator's function tree buffered.

On observed collection, `TYPE lifetime` is a standalone root with `code.type.name`, `otelc.lifetime.boundary="collection"` and a process-unique `otelc.lifetime.instance` span attribute. When creation occurred under a sampled otelc function, one link identifies that creation span. Each lifetime root uses the common root sampler independently; an unsampled or rejected creator omits the link rather than suppressing that lifetime root. Duration uses a monotonic clock. Sampling does not change admitted/completed/live accounting. Metrics use the shared histogram boundaries and sticky enable state captured at admission; live metrics controls are described in the [JavaScript guide](javascript.md#metrics-and-live-controls).

`runtime.max_live_lifetimes` bounds instance records. `runtime.max_functions` separately bounds type identities and concurrently tracked constructor identities, including dynamically created classes at the same source site. Weak constructor metadata is released on observed collection or shutdown. The existing trace record and export queue bounds also apply. Reports and `otelc.lifetime.dropped_observations` expose capacity, unsupported constructor, duplicate/stale completion, hook failure and incomplete losses. No per-instance metric label or raw address is exported.

Objects still tracked at shutdown are censored: current-live remains visible, metadata is released and no completion time/span is invented. Constructor-body throws preserve the original thrown value and increment failed initialisations; field-initializer throws happen before body probes and are not included in that failure counter, but never admit a lifetime.

## Qualified scope and remaining work

The initial scope is Node 24.21.0 on macOS ARM64, ESM/CommonJS ordinary base classes, fields/private fields, early void returns, default constructors, class expressions, aliases, static construction and collectible cycles. Selected inheritance and constructors returning values are refused during transformation. Using an observed base constructor through an unqualified subclass or proxy new-target preserves application behaviour but reports an unsupported-constructor loss instead of claiming a lifetime. Function-style constructors, decorators, browser/worker hosts, object destruction and explicit resource-release spans need separate mechanisms/qualification.

TypeScript uses the same observer after both Microsoft compiler-emission paths; see the separate [TypeScript collection guide](typescript-collection.md) for type identities, emission fixtures and boundaries. Current function metrics/spans and TypeScript native compiler support are unchanged. This milestone requires the complete local gate, independent minimum 80% lifecycle component coverage, oteleq observation qualification, code review and required exact-head CI before merge. Those results must be reported against the final published candidate; implementing these probes alone is not delivery.
