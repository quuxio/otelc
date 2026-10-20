# TypeScript collection lifetime spans

Ordinary TypeScript base classes can use the [Node collection observer](javascript-collection.md) after either Microsoft compiler emits private JavaScript. Application and dependency source stay unchanged. The measured interval is successful constructor completion to **observed garbage collection**, with bounded weak metadata, independently sampled lifetime roots and optional creation-span links. Collection is not destruction or resource release; shutdown censors surviving objects without inventing completion.

## Run the unedited and annotated examples

Build from the repository root and start the [Collector, Tempo, Prometheus and Grafana stack](observability-stack.md):

```sh
make build
make stack-up
./target/debug/quux-otelc --config examples/typescript-collection.toml ts examples/apps/typescript_collection_app.mts
./target/debug/quux-otelc --config examples/typescript-collection.toml ts examples/apps/typescript_collection_annotated.cts
```

Both print `48`. The example modules retain their two instances, so their reports show two admitted and two censored lifetimes; they do not promise completed collection spans. Tests use a separate actual-GC driver with Node's `--expose-gc`; production instrumentation never forces collection. Set `OTELC_REPORT_PATH` to retain accounting, and see the [JavaScript collection guide](javascript-collection.md) for metric names, loss reasons, bounds and Grafana queries.

The unedited source uses ordinary types and parameter properties:

```typescript
class WorkItem<T extends number> {
  constructor(public readonly value: T) {}
  compute() { return this.value * 3; }
}
function make(value: number) { return new WorkItem(value); }
export const items = [make(7), make(9)];
console.log(items.reduce((sum, item) => sum + item.compute(), 0));
```

The existing-annotation example contains a class comment. No annotation is required when configuration selects the class:

```typescript
// otelc.instrument
class AnnotatedItem {
  constructor(public readonly value: number) {}
  compute() { return this.value * 3; }
}
```

The shared policy selects types independently of functions:

```toml
[lifetimes]
enabled = true
boundary = "collection"
include = ["*.WorkItem"]

[traces]
enabled = true

[adapters.typescript]
backend = "source"
```

`source` uses the pinned Microsoft 6.0.3 API compiler. To use the pinned Microsoft 7.0.2 native executable instead, copy the example policy and change its existing `backend = "source"` line to `backend = "native"`; run the same `ts` commands with that policy. See [native compiler setup](typescript-native.md). Do not add a duplicate TOML table. `doctor` reports the selected compiler. The same policy defaults, annotation reading, exclusion precedence, bounds, metric controls and trace exporter apply to both paths.

## Emission and boundaries

Type identities, original line numbers and class annotations are recorded before lowering namespaces, private fields or parameter properties. Private constructor directives survive compiler emission and are removed before module execution; in-memory probes then reuse the JavaScript weak observer. No property is added to instances or prototypes. Classes outside type selection receive ordinary compiler emission, and files outside source selection are compiled without lifetime probes. Typed inspection currently lists selected collection classes; it is not an exhaustive type catalogue.

The qualification matrix covers Node 24.21.0/macOS ARM64, both pinned compilers, ES2018/ES2022, `.ts`/`.mts`/`.cts`, ESM/CommonJS, generic classes, parameter properties, private fields, namespaces, class expressions, implicit constructors and original throw identity. Actual-GC fixtures include cycles and user finalisers, and compare untouched source and stdout/stderr/status with plain compiler execution. Compiler helpers and erased ambient declarations are not application classes.

Selected inheritance, class/member/parameter decorators, value-returning constructors and direct constructor `eval` are refused before that module executes. An unqualified subclass/proxy new-target cannot produce a claimed base lifetime. Browser hosts, workers, general decorators/inheritance, explicit resource release and automatic destruction remain separate capabilities. Erased interfaces and type declarations have no runtime lifetime. The complete local gate, per-component 80% coverage, independent oteleq observations, code review and required final-head CI remain delivery requirements; a passing compiler fixture alone is insufficient.
