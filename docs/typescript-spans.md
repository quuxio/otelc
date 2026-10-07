# TypeScript function spans

The common schema-2 policy enables TypeScript function spans through compiler emission and the external Node loader. Original `.ts`, `.mts` and `.cts` files remain unchanged. The pinned compiler retains original names, annotations and source maps; emitted compiler helpers receive no application probes. Imported files outside source selection are compiled without instrumentation.

## Run and view

Start the [Collector, Tempo and Grafana stack](observability-stack.md), then run:

```sh
make build node-check
docker compose up -d
node --import ./adapters/node/plain.mjs examples/apps/typescript_trace_app.mts
./target/debug/quux-otelc --config examples/typescript-traces.toml --language typescript doctor
OTELC_REPORT_PATH=/tmp/typescript-traces.json \
  ./target/debug/quux-otelc --config examples/typescript-traces.toml \
  ts examples/apps/typescript_trace_app.mts
```

Both runs print `trace results preserved`. The [unchanged typed example](../examples/apps/typescript_trace_app.mts) produces eleven invocations in five trees, including recursion, direct async parenting, a bare Promise return and an escaping original exception. The [external policy](../examples/typescript-traces.toml) selects it without source annotations or an SDK import.

```ts
export function child<T>(value: T): T { return value; }
export async function asyncChild<T>(value: T): Promise<T> {
  await Promise.resolve();
  return child(value);
}
export async function asyncParent<T>(value: T): Promise<T> {
  return asyncChild(value);
}
```

Open the [trace dashboard](http://localhost:3000/d/otelc-traces) and set **Service** to `otelc-typescript-traces`. Inspect stored parent links and error status; an exporter acknowledgement alone does not prove Tempo storage.

## Existing annotations and compiler boundaries

Optional comments retain the selection rules in the [TypeScript 101 guide](typescript.md#optional-annotations-and-generated-code). For example:

```ts
// otelc.instrument
function selected(value: number): number { return value * 3; }
// otelc.exclude
function excluded(value: number): number { return value - 1; }
```

With `annotations.read_existing=true` and an empty function include list, only `selected` is instrumented. An annotation on the first declaration does not select unrelated declarations through the source-file node. Overload signatures have no executable body. ESM and CommonJS, parameter properties, namespaces, standard decorators and original function identities are qualified with trace export. Tests cover ES2018 and ES2022 emission; targets before ES2018 are rejected because they downlevel async and generator boundaries.

The shared [JavaScript span contract](javascript-spans.md#behaviour-and-boundaries) defines SDK IDs/sampling, complete-tree limits, independent transport/headers, strict Promise observation, shutdown health and metric controls. Traces stay enabled when live metric admission is disabled. Selected async generators, delegated yields, `for await` and dynamic `with` scopes reject explicitly; excluded sources remain uninstrumented. Automatic task/distributed propagation, manual SDK parenting, links and lifetimes remain unavailable. Settled Promise observation can briefly extend value/rejection lifetime. Production span overhead has no acceptance threshold yet.

The [existing compiler restrictions](typescript.md#compiler-and-module-boundaries) still apply: this adapter emits single modules and is not a project type checker. Keep the normal type-checking/build step. Browser bundles, JSX, declaration-only input, path aliases and decorator metadata are not qualified.
