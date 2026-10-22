# JavaScript and Microsoft TypeScript source launch

<!-- markdownlint-disable MD013 -->

The Node source profile is a rebuild candidate. It launches ordinary files with the same schema-3 policy as Java. Developers add no telemetry imports, annotations or bootstrap. `plan` lists Java under `implemented` and JavaScript/TypeScript under `candidates` while final language qualification, launch provenance and benchmarks remain open.

## Run the equivalent examples

Use Node 24.11+ and the native build tools needed by the observer. The Microsoft native compiler 7.0.2 and API compiler 6.0.3 are locked production dependencies. The adapter generates private compiler output and loads the application at its original URLs.

```sh
make setup node-observer node-http
make stack-up
.venv/bin/python -m otelc example javascript
.venv/bin/python -m otelc example typescript
```

Enter `10`, then `quit`. Both ordinary examples produce checksum `41284864`. Use `--plain` after either example language for its baseline. The TypeScript baseline still uses the selected real compiler, with probes absent. The example runner keeps its temporary files until the independent SDK drain completes. For retained reports or an existing application, use an explicit runtime directory below.

## Run an existing application

An ordinary application can look like this:

```typescript
function workload(value: number): number {
  return value + 1;
}
console.log(workload(Number(process.argv[2])));
```

For JavaScript, remove the type annotations. The same common configuration applies to either version:

```toml
schema_version = 3

[resource]
service_name = "ordinary-app"

[sources]
include = ["**"]

[functions]
include = ["*.workload"]

[metrics]
collection = true
emission = true

[export]
endpoint = "http://127.0.0.1:4320"
```

Save that policy as `otelc.toml`. From the otelc checkout, supply the original project root and entry file. Runtime files must go in a new private directory; the launcher refuses occupied generated files.

```sh
.venv/bin/python -m otelc --config /path/to/otelc.toml node \
  --language typescript --root /path/to/project --entry /path/to/project/app.ts \
  --runtime /tmp/otelc-app-run -- 7
```

Select `--language javascript` and a JavaScript entry for the equivalent JS application. `--backend api` selects the pinned Microsoft API compiler instead of the default native compiler. CJS/ESM and `.cts`/`.mts` files use their qualified execution formats. Arguments after `--` reach the application unchanged. The launcher inherits application stdin/stdout/stderr and adds a control-descriptor announcement to stderr.

Runtime-version probes and producer build/packaging helpers omit `NODE_OPTIONS` and `NODE_PATH`. They must not execute an application's preloads or resolve producer dependencies through application module paths. The actual application retains both settings; its existing preloads run once at application startup. The independent SDK process also omits these application settings.

Use another fresh directory and add `--plain` before `--` for a baseline. Plain JavaScript uses Node directly; plain TypeScript uses the real compiler without starting the SDK or installing probes. No forced GC is added by these public launch commands.

## Change metrics in a running process

For a long-running application, use its announced descriptor with the same common commands as Java:

```sh
.venv/bin/python -m otelc metrics --socket /tmp/otelc-app-run/control.json status
.venv/bin/python -m otelc metrics --socket /tmp/otelc-app-run/control.json off
.venv/bin/python -m otelc metrics --socket /tmp/otelc-app-run/control.json collection on
.venv/bin/python -m otelc metrics --socket /tmp/otelc-app-run/control.json emission on
.venv/bin/python -m otelc metrics --socket /tmp/otelc-app-run/control.json flush
```

The descriptor is an authenticated loopback endpoint, despite the shared flag name `--socket`. Collection and emission are independent; both hooks are installed at launch even if they start off. Traces retain their launch-time policy. Emission-off stops new metric exports; wait for `effective_emission_off` before measuring a strict export-off interval.

## Completion and current limits

The application process can exit before the independent SDK finishes exporting. The CLI waits for the bounded final report before it returns or removes example runtime files. Explicit-runtime launches retain `node-plan.json`, SDK diagnostics and `node-status.json`. A successful final report has `phase = "closed"`; pipeline failure or a missing report makes an otherwise successful CLI launch fail. An existing non-zero application result is retained. The report distinguishes parent/SDK PIDs, producer handoff loss, incomplete observations and SDK queue evidence. Export callbacks alone do not prove downstream receipt. The separate Java `health` report protocol is not yet implemented for Node.

This profile covers the tested Node runtime/compiler paths. Browser instrumentation, fetch/Undici, application-worker propagation, general build-system discovery and complete prepared-launch provenance remain work. Selected async generators, delegated yields, for-await tracing, direct eval and some lifetime/source constructs are refused or reported as unsupported. Object lifetimes are construction-to-observed-GC or matched successful resource release; they do not imply immediate destruction. Final cross-language benchmark and Grafana comparison qualification is still pending.
