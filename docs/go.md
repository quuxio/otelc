# Go instrumentation 101

The Go adapter parses original source with Go's AST and adds deferred probes through compiler overlays. Original `.go`, `go.mod` and `go.sum` files stay unchanged. A private alternate module manifest supplies the pinned Go OpenTelemetry SDK; the application needs no imports, annotations or manual probes.

## Install and run

Use Go 1.26+; the local and CI qualification uses Go 1.27.1. Install the pinned validation tools, build the launcher and start the [metrics stack](observability-stack.md):

```sh
make go-tools
export PATH="$PWD/build/go-tools:$PATH"
make build go-check
./target/debug/quux-otelc --config examples/go.toml --language go doctor
./target/debug/quux-otelc --config examples/go.toml --language go inspect examples/apps/go_app.go --json
go run examples/apps/go_app.go
./target/debug/quux-otelc --config examples/go.toml go examples/apps/go_app.go
```

Both runs print `72`. Instrumentation records ten calls, including recursive calls, methods and goroutine work. The escaping panic counts as an unwind; the function that recovers its own panic counts as a completed call. The adapter also accepts a package target such as `go ./cmd/server [APP_ARGS...]` inside an existing module. Application arguments and exit codes are preserved.

An unchanged function is ordinary Go:

```go
func process_order(value int) int { return value * 3 }
```

The common configuration selects it externally:

```toml
schema_version = 2
languages = ["go"]
[sources]
include = ["examples/apps/go_*.go"]
[functions]
include = ["examples.apps.go_app.process_order"]
[adapters.go]
backend = "compile"
```

Identities use original project-relative source paths without `.go`, followed by the function or receiver/method name. For example, `examples.apps.go_app.Order.calculate`. Anonymous callbacks include original line and column; initialisers include their file and line to avoid collisions. Inspection exposes these exact names. Selection/exclusion uses the common byte-glob semantics, including Unicode names.

## Optional comments and generated input

The annotated example requires no SDK import:

```go
// otelc.instrument
func selected(value int) int { return value * 3 }

func configured(value int) int { return value + 7 }

// otelc.exclude
func excluded(value int) int { return value - 1 }
```

```sh
./target/debug/quux-otelc --config examples/go.toml go examples/apps/go_annotated.go
```

It prints `30` and records two calls. `annotations.read_existing = true` enables declaration comments and adjacent comments on body-local callbacks. External exclusions and `otelc.exclude` win. Package-level callback bodies are selectable through configuration. Unknown instrumentation comments fail clearly. `inject_generated = true` adds optional metadata only to generated input; generated input cannot be instrumented again.

The generated compiler input follows this pattern with a unique import binding:

```go
func selected(value int) int {
    defer __quux_otelc.Finish(__quux_otelc.Start("examples.apps.go_annotated.selected"))
    return value * 3
}
```

The original function is not wrapped. Existing defers run before the timing defer, preserving named return updates and direct recovery. The timing defer records an escaping panic and rethrows the same value. Package-level callbacks, generic functions and receiver methods retain their original bodies. Original line directives preserve source locations. The generated main body also defers bounded SDK shutdown, even when main is excluded from timing.

## Live controls and latency

The service is `otelc-go-example`. The SDK exports the shared call/unwind counters and configured seconds histogram through the Collector to Prometheus and Grafana. Resource attributes, endpoints/header precedence, capacities and deadlines come from the same common policy as the other languages.

Set `runtime.control_socket` inside an owner-only directory, then use `status`, `enable` and `disable` from the [developer guide](developer-101.md). The socket is owner-only and reports the application PID. Calls admitted before disable finish; disabled calls do not record. No restart or rebuild is needed.

```sh
make benchmark-language LANGUAGE=go LANGUAGE_BENCHMARK_ARGS="--output build/benchmarks/go --iterations 10000 --runs 8"
```

The benchmark compares original execution, disabled probes and enabled metrics, preserving one instrumented PID. It checks matching results, original hashes, exact counts, complete export and zero losses. `make go-check` runs formatting, vet, Staticcheck, govulncheck, race detection and an 80% product statement-coverage gate. Coverage includes the overlay adapter, policy, runtime and launcher.

## Build and runtime boundaries

Only project packages are transformed; third-party and standard-library sources remain untouched. Existing module manifests are copied into a private alternate manifest. Dependency resolution can raise shared dependency versions in that private build graph to meet the pinned SDK's requirements; run normal application regression tests as well. Standalone files with standard-library imports are supported without creating a module in the original project. Temporary build files are removed after execution.

Go workspaces, project cgo packages, vendor mode, conflicting overlay/modfile/toolexec/buildmode flags and older compilers are rejected pending qualification. `GODEBUG=panicnil=1` is rejected because legacy nil panic recovery cannot preserve both panic propagation and normal-return classification. If an application enables that legacy mode dynamically, the probe avoids recovery, preserves propagation and reports an `unsupported_runtime` loss.

Duration includes deferred cleanup. `runtime.Goexit` runs defers and records completion; it has no normal-return claim. `os.Exit`, a fatal panic in another goroutine and forced termination cannot guarantee final export. SDK admission and pending calls are bounded; incomplete calls and capacity losses remain explicit. Automatic object lifetimes, spans and context propagation remain unavailable and are rejected.

The pinned SDK performs aggregation and protobuf encoding. The bounded HTTP transport permits serial exports, requires a decoded HTTP 200 acknowledgement, rejects redirects, malformed/partial acknowledgements and bodies over 64 KiB, closes rejected responses without waiting for their bodies, closes idle connections at shutdown and keeps response content out of diagnostics. Failed cumulative snapshots can be retried at the next interval. Go vulnerability analysis checks compiled package/call-graph exposure: the SDK's broader module graph includes the deprecated OpenPGP advisory, but no OpenPGP package is imported by this adapter or runtime.

Duplicate export suppression fingerprints the collected cumulative SDK snapshot, with observation timestamps removed and health counters retained. Export loss without a new application call still changes the next exported snapshot. A concurrent completion cannot label an older snapshot as current or suppress its successor. Admission rechecks live enable state after acquiring the runtime lock.

Development analysers are pinned separately in `ci/go-tools/go.mod` and `go.sum`. `make go-tools` builds them with `-mod=readonly`; CI and `make go-check` use these locked binaries. The development tool build uses Go 1.27.1.
