# TypeScript 7 native compiler

Use the common schema-2 policy to opt into Microsoft’s TypeScript 7.0.2 executable compiler. Application and dependency source files remain unchanged. The Node runtime still provides function metrics, sampled spans and live metrics controls; this backend does not add a browser host or automatic lifetimes.

```toml
schema_version = 2
languages = ["typescript"]
[sources]
include = ["examples/apps/typescript*.*"]
[functions]
include = ["examples.apps.typescript_app.*"]
[adapters.typescript]
backend = "native"
```

The complete [`examples/typescript-native.toml`](../examples/typescript-native.toml) also configures existing annotations, service identity and export. The default `source` backend remains TypeScript 6.0.3 API emission. No ambient environment variable changes the instrumented backend: choose it in the common policy.

## Run unchanged and annotated examples

Install the locked adapter dependencies and build its Promise observer for your Node executable:

```sh
make setup build node-build
./target/debug/quux-otelc --config examples/typescript-native.toml --language typescript doctor
OTELC_TYPESCRIPT_BACKEND=native node --import ./adapters/node/plain.mjs examples/apps/typescript_app.mts
./target/debug/quux-otelc --config examples/typescript-native.toml ts examples/apps/typescript_app.mts
./target/debug/quux-otelc --config examples/typescript-native.toml ts examples/apps/typescript_annotated.cts
```

The unchanged module prints `75` and records 18 instrumented calls; the annotated CommonJS example prints `30` and records two. The [TypeScript 101](typescript.md#optional-annotations-and-generated-code) shows its source comments. Annotations remain optional, exclusions win and compiler-generated helpers are excluded from the function inventory.

`doctor` distinguishes the native 7.0.2 emitter from the 6.0.3 identity parser. Microsoft’s [TypeScript 7 release notes](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/) describe the native compiler and the separate compiler API transition. The adapter invokes the pinned platform executable directly; it does not depend on an unstable native JavaScript compiler API.

## Private emission and source maps

The 6.0.3 parser reads original function identities, source positions, optional annotations and root `tsconfig.json`. The native compiler emits one private module at a time with `noCheck` and `noResolve`; the Node loader separately loads project-local imports. Run your normal project type checker and build as well.

Temporary directories are private and outside the canonical source project. Native emission cannot write the project’s output directory or incremental build cache. The adapter removes temporary input and output after each emission, including compiler failures. A compiler invocation has a 30-second timeout and a bounded diagnostic buffer. The executable and platform package must both match the pinned release.

Private identity comments or string markers survive native lowering. String markers are removed before probes or application execution, preserving original directives. Source maps compose identity insertion, native emission and probe insertion back to the original TypeScript, including original source content. Parameter-property constructors and namespace functions have a regression test because the native compiler can discard their leading comments.

ESM `.mts`, CommonJS `.cts`, original overloads, enums, namespaces, decorators, typed imports, parameter properties, getters/setters and anonymous callbacks have focused fixtures. ES2018 and ES2022 identity inventories agree with the classic compiler. Legacy and standard decorators have separate checks. Existing shared Node tests cover async completion, rejection and generator boundaries; the native examples exercise those emitted paths too.

The [existing compiler restrictions](typescript.md#compiler-and-module-boundaries) apply: no JSX, declarations as executable inputs, path aliases/baseUrl, decorator metadata, bundled output or AMD/System/UMD formats. Syntax unknown to the identity parser fails explicitly even if the native compiler would accept it. Node 24 on macOS ARM64 is the qualified host; other native compiler platforms, browser bundles and alternative JavaScript hosts require separate qualification.

## Traces and paired latency measurement

Start the [Collector and Grafana stack](observability-stack.md), then run:

```sh
make node-check
./target/debug/quux-otelc --config examples/typescript-native-traces.toml ts examples/apps/typescript_trace_app.mts
make benchmark-language LANGUAGE=typescript LANGUAGE_BENCHMARK_ARGS="--native-typescript --output build/benchmarks/typescript-native --iterations 10000 --runs 8"
```

The trace fixture produces 11 spans in five trees, including one escaping error. Function selection and telemetry policy remain common across both TypeScript backends. Live `enable` and `disable` affect metrics admission in the same process without rebuilding; trace sampling remains separately configured.

`--native-typescript` selects the same native compiler for the plain baseline and instrumented workload. `OTELC_TYPESCRIPT_BACKEND=native` is only the compiler-only baseline’s switch. The benchmark retains raw baseline, metrics-off and metrics-on timings, source hashes, PID, exact call counts, complete export and losses. Native compiler subprocess time occurs during module loading and is outside steady-state call timings. Per-module subprocess startup is a trade-off of using the executable compiler; this is not a project compilation speed benchmark.

Local qualification used Node 24.21.0, both examples, stored Collector/Tempo spans and a four-batch metrics benchmark with 10,000 calls per batch. It observed the expected 41,000 calls, unchanged source, matching checksums, the same instrumented PID and zero loss. These fixed fixtures do not establish production overhead or equivalence of all hidden application state. Keep the report from your own workload and use the independent oteleq qualification for declared observable channels.
