# Retained Go builds and replay

Build an ordinary baseline and an instrumented Go executable once, then run either without compiling again. Application and dependency sources, `go.mod` and `go.sum` stay unchanged. The common schema-2 policy selects functions and controls the SDK.

This is a host-specific diagnostic build bundle, not a signed attestation or a general deployment package. Current qualification uses macOS ARM64 and Go 1.27.3; other hosts/toolchains require separate qualification. Runtime assets used by the application must still be available. Replay needs the original adapter binary and host target, but no Go compiler, module cache, application source or SDK source installation for the qualified self-contained fixtures.

## Source, configuration and commands

The existing [Go application](../examples/apps/go_app.go) is ordinary source:

```go
func process_order(value int) int { return value * 3 }
```

No SDK import or manual probe is required. The [Go example policy](../examples/go.toml) selects functions externally and enables optional existing comments. The generated compiler overlay contains the probes; the original file is unchanged.

From the repository root, with the qualified Go 1.27.3 toolchain:

```sh
make build go-check
make stack-up
mkdir -p build

./target/debug/quux-otelc --config examples/go.toml go \
  --build examples/apps/go_app.go --output-dir build/go-app-bundle
./target/debug/quux-otelc --config examples/go.toml go \
  --run-baseline build/go-app-bundle
./target/debug/quux-otelc --config examples/go.toml go \
  --run build/go-app-bundle
```

Both executions print `72`. Build-only never executes application `init` or `main`, and emits no application telemetry. Additional arguments follow the bundle directory and are forwarded unchanged, as are stdin, stdout, stderr, ordinary exit codes and Unix signal termination. Forced termination and `os.Exit` do not guarantee final telemetry export.

Use [the Go span policy](../examples/go-traces.toml) to build a trace-enabled bundle. Enable tracing before building: Go currently emits different probes for metrics-only and trace-enabled policies.

## Runtime changes and benchmarking

Each replay resolves the common configuration again. Metrics enable state, histogram boundaries, service/resource attributes, export settings, runtime capacities/control socket, trace sampling and trace limits can change without rebuilding. They take effect on the next launch. The existing [live control socket](go.md#live-controls-and-latency) can enable/disable metrics in a running process.

Source/function selectors, annotation settings, backend and `traces.enabled` are build settings. Changing them refuses replay before application execution and requires a new bundle. Explicit compiler/target environment changes such as `GOFLAGS`, `GOOS`, `GOARCH` and `CGO_ENABLED` are also refused. Workspaces remain unsupported.

Use `--run-baseline` and `--run` for repeated process measurements without compilation in either lane. Keep startup measurements separate from steady-state call timings: replay validates the bundle and copies the selected executable into a private directory before launching. Both lanes use the same validation path. The [Go live benchmark](go.md#live-controls-and-latency) measures disabled/enabled metrics in one instrumented process; build time is not a per-call metric cost.

## Retained evidence and validation

`build.json` is published last and hash-binds the matching adapter and every listed file. The bundle contains:

- `baseline` and `application`: ordinary and instrumented executables.
- `plan.json`: the resolved build policy; replay compares its build settings with the current resolved policy.
- `recipe.json`: actual compiler driver/tool hashes, version, target/environment, baseline and instrumented argument vectors, embedded Go build information, and hashes of loaded application, standard-library, SDK and dependency inputs. SDK module/lock and imported policy identities are included. Absolute original paths are provenance, not required replay paths.
- `baseline.mod`/optional `baseline.sum`, private `go.mod`/optional `go.sum`, and original/resolved module snapshots: the real module recipes and selected dependency identities.
- `overlay.json` and generated `source-N.go` files: exact overlay bytes used by compilation. Recorded compiler arguments retain the original private build paths; they are diagnostic records, not a relocated compiler command.
- Baseline/instrumented build stdout, stderr and exit-status records.

Replay verifies listed hashes and regular-file requirements, adapter identity, both executables' embedded build information, module preservation, host target and current build policy before launching a private copy. Altered executable/overlay bytes, unsupported schema, missing required evidence, mismatched adapter, unsafe paths, metadata symlinks and incompatible build settings fail before application execution. Hashes detect inconsistent bundles; an actor able to replace all files and recompute their metadata is outside this diagnostic trust boundary.

Metadata files are bounded to 4 MiB, executables/hashed inputs to 256 MiB per file, and input/file inventories to 65,536 entries. Output must be a new directory with an existing parent; failed publication removes its own incomplete directory. Existing directories and dangling output symlinks are refused. Private directories use owner-only permissions. No compiler cache is copied into the bundle.

The [module-preservation rules](go.md#build-and-runtime-boundaries) apply: existing dependency/language/default identities must survive SDK resolution or the build is refused. SDK dependency isolation, workspaces, project cgo and project assembly/object inputs need separate qualification. Automatic lifetimes and task/distributed propagation remain unavailable.

`make go-check` enforces formatting, vet, Staticcheck, vulnerability and race checks. Module preservation, recipe recording, artifact validation, replay and bounded file handling each have an independent 80% statement-coverage floor. Real tests assert no build-time execution/export, source/module preservation, byte/argument/status/signal parity, compiler-free replay, runtime-only changes and decoded SDK telemetry. Independent oteleq qualification and required current-head CI must complete before this new capability is merged.
