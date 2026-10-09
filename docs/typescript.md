# TypeScript instrumentation 101

The TypeScript adapter emits JavaScript from original application modules, then inserts function timing probes. The default `source` backend uses the pinned TypeScript 6.0.3 compiler API in memory. The opt-in [`native` backend](typescript-native.md) uses Microsoft’s TypeScript 7.0.2 executable compiler on private copies, retaining the 6.0.3 parser for original identities and configuration. The compiler preserves types, overloads, enums, namespaces, decorators and parameter-property semantics. Application and dependency files stay unchanged. Compiler-generated helper functions do not become application metrics.

## Install and run

Use Node 24.11+ and the repository setup. No original decorators, imports or runtime calls are required for instrumentation.

```sh
make setup build
./target/debug/quux-otelc --config examples/typescript.toml --language typescript doctor
./target/debug/quux-otelc --config examples/typescript.toml --language typescript inspect examples/apps/typescript_app.mts --json
node --import ./adapters/node/plain.mjs examples/apps/typescript_app.mts
./target/debug/quux-otelc --config examples/typescript.toml ts examples/apps/typescript_app.mts
```

Both executions print `75`. The instrumented run records 18 calls with no loss, including recursive calls, async completion/rejection, generators, constructors, private methods and a namespace function. The compiler-only baseline uses the identical TypeScript compiler and module pipeline with no SDK or timing probes; it works for syntax such as enums that Node's native type stripping cannot execute.

The unchanged typed function looks like ordinary application code:

```ts
interface Item { value: number; }
type Identity<T> = T;
export function process_order<T extends Item>(item: Identity<T>): number {
  return item.value * 3;
}
```

`examples/typescript.toml` uses the common schema-2 policy and `source` backend. Function names use original project-relative filenames, class/namespace/function names and original source lines for anonymous callbacks. For example, `examples.apps.typescript_app.process_order` and `examples.apps.typescript_app.Rules.bonus`. Original overload signatures have no body and do not create duplicate observations. Inline source maps compose compiler emission and probe insertion back to original TypeScript.

## Optional annotations and generated code

The annotated `.cts` example needs no runtime import:

```ts
// otelc.instrument
function selected(value: number): number { return value * 3; }

function configured(value: number): number { return value + 7; }

// otelc.exclude
function excluded(value: number): number { return value - 1; }
```

```sh
./target/debug/quux-otelc --config examples/typescript.toml ts examples/apps/typescript_annotated.cts
```

It prints `30` and records two calls. `read_existing = true` recognises the comments; external exclusions and `otelc.exclude` win. Unknown instrumentation comments fail for selected source files. Comments are optional selection metadata, separate from application decorators that the TypeScript compiler already handles.

The generated JavaScript body follows this pattern, with a unique helper binding supplied by the adapter:

```js
function selected(value) {
  const token = probes.enter('examples.apps.typescript_annotated.selected');
  let unwound = false;
  try { return value * 3; }
  catch (error) { unwound = true; throw error; }
  finally { probes.exit(token, unwound); }
}
```

The original `.cts` file is never rewritten. `annotations.inject_generated = true` adds optional metadata to the generated probe, and repeated generated input is rejected.

## Live metrics and latency

The [JavaScript guide](javascript.md#metrics-and-live-controls) describes the shared owner-only socket and `status`, `enable`, `disable` commands. These work for TypeScript inside the same application PID without rebuilding or relaunching. Calls admitted before disable finish; calls begun while disabled stay unmeasured.

Start the [Collector, Prometheus and Grafana stack](observability-stack.md), then run:

```sh
make node-check
make benchmark-language LANGUAGE=typescript LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/typescript --iterations 10000 --runs 8"
```

The service is `otelc-typescript-example`. The common counters and seconds-based histogram, configured bounds, endpoint/header precedence, export limits and loss reporting are identical to JavaScript. The benchmark checks original hashes, matching results, exact call counts, complete export, zero loss and the same instrumented PID before writing its report. It separates baseline, disabled probes and incremental metrics latency. Product coverage must remain at least 80%.

## Compiler and module boundaries

The adapter reads root `tsconfig.json`, including extended compiler settings. It preserves target and decorator settings, with an ES2022 default and a minimum ES2018 target to keep async/generator timing intact. Node module format follows `.mts`/`.cts`, package format and module syntax; the adapter emits for Node rather than using an AMD/System/UMD bundle setting. Relative import extensions are preserved in both emitters, overriding `rewriteRelativeImportExtensions`, because the loader executes modules at original source URLs. Project-local typed imports are compiled even when source filters exclude their probes. Third-party `node_modules` files remain outside instrumentation.

JSX, declaration-only inputs, path aliases/baseUrl, decorator metadata emission, bundled outFile and non-Node module formats are rejected clearly. TypeScript compilation here is single-module emission, not project type checking: run your normal type checker/build as well. The native compiler is separately pinned and qualified; syntax must also be understood by the 6.0.3 identity parser. Neither backend promises project-wide type checking.

The JavaScript boundaries for direct eval, top-level CommonJS require shadowing, async/generator timing, forced termination, workers and custom loaders also apply. Timing begins in the body after argument/default initialisation. Sampled function spans are now available through the common policy; see [TypeScript spans](typescript-spans.md). Automatic lifetimes and browser bundles remain unavailable and are rejected rather than silently omitted.

Function identities distinguish object bindings, getter/setter methods and same-line anonymous callbacks by their original source position. Returned promises follow the same completion rules as the JavaScript adapter.

Async functions preserve original Promise adoption, getter access, cleanup, rejection identity and microtask ordering with metrics enabled and disabled. The [shared native Promise observer](javascript.md#optional-annotations) runs on the emitted JavaScript and adds no await or Promise handler. Rebuild its addon for the exact Node version after upgrades. The same bounded origin, stack-depth and dynamic-script limitations apply and produce visible observation losses.

Measure async completion separately from the synchronous benchmark:

```sh
make benchmark-language LANGUAGE=typescript LANGUAGE_BENCHMARK_ARGS="--node-async --output build/benchmarks/typescript-async --iterations 10000 --runs 8"
```

This uses the unchanged `typescript_async_latency.mts` workload and the same compiler-only baseline. Stack observation overhead is workload dependent; complete reports require exact counts and zero losses.
