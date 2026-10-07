# JavaScript instrumentation 101

The Node adapter transforms selected application functions in memory. Original JavaScript and dependencies stay unchanged. It inserts timing inside function bodies rather than replacing function objects, preserving parameters, `this`, constructor calls and return/throw behaviour. CommonJS and ES modules use the same schema-2 policy and JavaScript OpenTelemetry SDK.

## Install and run

Use Node 24.11+ (validated locally with Node 26.10), Rust 1.98+ and the repository setup. Run from the project root so source paths and qualified function names have a stable base.

```sh
make setup build
./target/debug/quux-otelc --config examples/javascript.toml --language javascript doctor
./target/debug/quux-otelc --config examples/javascript.toml --language javascript inspect examples/apps/javascript_app.mjs --json
node examples/apps/javascript_app.mjs
./target/debug/quux-otelc --config examples/javascript.toml node examples/apps/javascript_app.mjs
```

Both executions print `70`. The configured run records 17 calls, including recursive calls, two escaping exceptions, generator completion, async completion, constructors, private methods and an application method whose parameters shadow JavaScript globals. No application imports or annotations are required.

The unchanged function is ordinary JavaScript:

```js
export function process_order(value) { return value * 3; }
```

The example policy includes `examples/apps/javascript*.*` and functions matching `examples.apps.javascript_app.*`. Function identities use the project-relative filename without its extension, then enclosing classes/functions and the declared function name. For example, `examples.apps.javascript_app.Order.calculate`. Object methods include their object binding or source position; getter/setter identities include `get`/`set`. Anonymous callbacks use their source line and column so callbacks on the same line remain distinct. Exclusion patterns always win, including against annotations. Use `inspect` to see names before running.

## Optional annotations

Existing comments can opt functions in without changing their body:

```js
// otelc.instrument
function selected(value) { return value * 3; }

function configured(value) { return value + 7; }

// otelc.exclude
function excluded(value) { return value - 1; }
```

This is the committed CommonJS example, with no runtime import. `read_existing = true` recognises `otelc.instrument` and `otelc.exclude`; unknown `otelc` comments fail clearly. `configured` is selected by the external policy. The annotated example prints `30` and measures exactly two calls:

```sh
./target/debug/quux-otelc --config examples/javascript.toml node examples/apps/javascript_annotated.cjs
```

Generated bodies have this shape; the helper binding is generated and imported by the adapter, and the original file is never rewritten:

```js
function selected(value) {
  const token = probes.enter('examples.apps.javascript_annotated.selected');
  let unwound = false;
  try { return value * 3; }
  catch (error) { unwound = true; throw error; }
  finally { probes.exit(token, unwound); }
}
```

Async functions adopt returned promises after original cleanup completes, so their duration and unwind count follow final promise settlement. Body-level function declarations retain hoisting, lexical captures and mutable self-reference when moved into generated timing blocks.

Unmeasured calls, including calls started with metrics disabled or rejected by a capacity limit, keep the original async return path and microtask ordering. Primitive async results also return without an extra suspension. Admitted async calls returning objects or functions add an `await` after cleanup to observe possible Promise/thenable settlement; this can change ordering relative to other queued microtasks. Applications that depend on that ordering need qualification with metrics enabled.

Inline source maps refer to the original filename and lines. `annotations.inject_generated = true` adds an annotation comment to the generated probe only. The generated marker rejects accidental repeated instrumentation.

## Metrics and live controls

Start the documented [Collector, Prometheus and Grafana stack](observability-stack.md). The example service is `otelc-javascript-example`; function counts, unwinds and duration histograms use the shared instrument names, seconds and configured histogram bounds. Exports use OTLP/HTTP protobuf and signal-specific endpoint/header precedence. Redirects, malformed success bodies, oversized responses and Collector rejections are failures. Credential headers are read from the environment, never the policy or reports.

Add an owner-only socket directory and runtime setting to a copy of the example policy:

```sh
mkdir -p build/javascript-control
chmod 700 build/javascript-control
```

```toml
[runtime]
control_socket = "build/javascript-control/metrics.sock"
```

While the process runs, use the existing CLI:

```sh
./target/debug/quux-otelc status --socket build/javascript-control/metrics.sock
./target/debug/quux-otelc disable --socket build/javascript-control/metrics.sock
./target/debug/quux-otelc enable --socket build/javascript-control/metrics.sock
```

The response identifies the application PID. Calls admitted before disable finish normally; calls started while disabled never produce observations. Selection changes still require a new launch. The exporter has at most one request in flight, bounded by the smaller of export timeout, export interval and shutdown timeout. Successful unchanged snapshots are suppressed. Runtime capacity, incomplete calls and failed exports are reported explicitly.

## Measure overhead and validate

```sh
make node-check
make benchmark-language LANGUAGE=javascript LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/javascript --iterations 10000 --runs 8"
```

The benchmark launches the same untouched workload plain and instrumented, alternates metrics off/on inside the same instrumented PID, and checks checksums, source hashes, exact observation counts, losses and final export completion. The report includes baseline overhead, disabled-probe overhead and incremental metrics cost per call. Results are measurements on this host, not a general latency guarantee.

`node-check` requires at least 80% product line coverage and exercises real loaders, SDK histogram/counter data, protobuf export, optional annotations, private control sockets, normal/exceptional exits and transport failures.

## Supported boundaries

This implementation targets project-local Node `.js`, `.mjs` and `.cjs` modules, including dynamic imports. It excludes dependency files under `node_modules`, the adapter itself and modules outside the project root. Timing begins inside the function body after parameter initialisation; async/generator timing includes suspension. An escaping rejection from an `async` function is an unwind; a regular function returning a Promise ends at its synchronous return. Generator abandonment is an incomplete observation rather than a fabricated completion.

Direct `eval` within selected functions and top-level CommonJS `require` shadowing fail clearly because transformation could change their lexical semantics. TypeScript, JSX, browsers, workers/child-process propagation, application custom loaders and packaged distributions are separate capabilities. Worker bootstrap does not re-use the main process's plan or socket. Natural process exit flushes metrics; forced termination, fatal uncaught exceptions and `process.exit()` cannot promise a final asynchronous export. Automatic object lifetimes and spans are unavailable and rejected by policy resolution.
