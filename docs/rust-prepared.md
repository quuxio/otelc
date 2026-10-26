# Prepared Rust execution candidate

<!-- markdownlint-disable MD013 -->

This explicit candidate separates original Rust compilation, owned preparation and host release. Both plain and instrumented lanes wait in the fixed native exec gate before executing the application or its constructors. Developers add no telemetry imports, annotations or startup code. The existing direct `rust-run` command keeps its direct-child behaviour.

The API implements the reserved `rust1981-owned-standalone-v1` profile around the [owned-build envelope](rust-owned-build.md). It accepts original sync-v1/source-build1/native-plan2/health1 or drop-v2/source-build2/native-plan3/health5. Each request, plan, preparation, supervisor, application, host-release and execution record has its own fixed Rust filename and schema1. Negotiation alone does not prove execution or independent provider qualification.

## Build and run

Create the original source build using the [Rust build guide](rust-source.md). Then wrap it in a new owned directory and run from another new private runtime. Only `rust-own-build` compiles a gate and requires at least 8 GiB free; preparation, verification, command generation, host release and execution reading run no compiler.

```sh
bin/quux-otelc rust-protocols --source-build-version 1
bin/quux-otelc --config examples/functions.toml rust-own-build \
  --build /work/rust-compiled --output /work/rust-owned
bin/quux-otelc --config examples/functions.toml rust-prepared-run \
  --build /work/rust-owned --runtime /work/rust-runtime
```

Use `--plain` for the stock image or `--prepare-only` to leave ownership and release to an external host. Literal application arguments follow `--`. The common policy still supplies function/source/type selection, independent live metric collection/emission, traces and export configuration; no second language-specific instrumentation policy is needed. Controls for an instrumented application are under `/work/rust-runtime/app/control.json`. The plain lane creates no SDK plan or health.

## External release host

`otelc.prepared_rust` exposes `protocols`, `prepare`, `verify`, `command`, `release` and `execution`. `prepare` accepts an owned build directory, literal argument tuple, lane and a 1..300000 ms host-wait budget. It creates a new mode0700 runtime with exclusive mode0600 records and starts no process. Arguments are limited to 4096 values and 1 MiB of filesystem-encoded bytes, with NUL refused. Runtime writes must remain outside original source/compiler/SDK and original/owned build input directories, including physical aliases.

The host starts the fixed isolated/no-site command in its own session. The separate supervisor creates a child in another private session/group and publishes `rust-application-start.json` with phase `waiting_exec`. Before release, the host must independently observe that actual child's PID, parent, process group and session. The ordinary CLI performs these OS checks itself. `release(runtime, pid=..., supervisor_pid=...)` publishes one acknowledgement tied to the launch ID. Existing start/release records cannot be replaced or replayed.

The child's PID, literal argv and streams remain unchanged across exec. A plan's `image` identifies the intended checked binary; it is not a kernel observation. Execution retains `exec_verified=false`. Independent consumers must collect their own image/source observations rather than infer them from SDK metadata.

`execution` binds the original request/plan, actual process records, host release, signed child return code and mapped supervisor exit. It rechecks original source, compiler/bridge/SDK products, gate, interpreter and producer files. Instrumented execution additionally binds the raw final `app/health.json` to the Rust PID, policy and compiled selection. Plain execution refuses SDK files. Failures remain explicit; an interrupted or failed execution cannot become clean through a release acknowledgement or successful SDK API call. Final health alone does not prove zero loss or downstream receipt.

## Validation and limits

`make rust-prepared-check` runs bounded record, identity, OS-host orchestration and supervisor failure tests without compiling applications. Separate 80% floors cover `prepared_rust.py` and `rust_supervisor.py`; the full Rust and repository checks enforce them too. `make rust-public-check` adds genuine original-source compiler/SDK tests for both lanes, a Rust constructor marker, literal arguments, all four live metric states and drop-profile outcomes. These hosted runtime checks and protected delivery must pass before those behaviours are described as verified.

The current public Rust suite retains bounded original HTTP entities under `otlp-http/`, before content decoding, with sequence, content encoding, size and SHA-256 metadata. Chunk framing is removed; request headers and credentials are not retained. The shared capture helper refuses more than 1024 requests or 64 MiB. It reuses the native benchmark diagnostic capture. Parsed protobuf messages stay in memory for assertions; redundant reserialized copies are no longer written. Earlier frozen records retain their decoded-only scope.

`make rust-receipt-check` exercises real HTTP retention and literal receipt/refusal cases without compiling applications, with separate 80% floors. Prepared fixtures check absent plain telemetry, disabled emission windows, retained collection on restoration, exact selected span/type/link counts and final engine admissions/completions. They require ratio-one tracing for the exact call denominator, cumulative monotonic counters and complete nonnegative duration fields. Duration sums, extrema, populated buckets and cumulative transitions use the unchanged [oteleq histogram observation model](https://github.com/quuxio/oteleq/blob/da94baf4ae460bc4a4b197924e0fa0b97ca57602/adapters/generation/histogram_observation.py), including its rational binary64 summation bound and concrete-prefix regression tests. Gauges retain their distinct observation-time semantics. This reused test-side reference model adds no application/runtime dependency on oteleq; independent consumer execution qualification remains separate.

After a clean SDK drain, received span/metric-point totals must match SDK units. Exporter API batches remain separate from HTTP request counts: empty metric API exports may send no request. Hosted uploads retain the referenced source-build/SDK metadata and all prepared CLI records, without compiler caches or SDK build trees.

Independent oteleq generated/retained execution qualification, original-wire telemetry, benchmark integration, full compiler/sysroot/dependency closure and shipping acceptance remain required. Cargo/build scripts/dependencies, general ownership and async/task/service propagation remain open. This candidate does not complete Rust language admission, and `source_complete=false` stays explicit.

## Separate generated callers

The existing source-build 1/2 contracts can compile a generated `drivers/main.rs` next to an unedited `original/application.rs`, beneath one explicit compilation root. An ordinary `#[path="../original/application.rs"] mod original;` declaration loads the physical original file. The generated caller can invoke its public functions; no telemetry imports or source edits are needed. Set `sources.include = ["original/**"]` and select the intended function names so the caller and its helpers remain uninstrumented. Build output stays outside the shared root. Callers must retain the original/generated distinction and freeze both input catalogues before compilation; the shared build root alone does not establish original-source provenance.

The hosted prepared fixture retains both physical source files, their pre-build identities, compiler requests/inventories, owned build and both execution lanes. It checks unchanged input identities, exact selected original function, plain output without telemetry, and the instrumented function receipt against engine counts. This is a bounded public-module recipe under the existing formats. Arbitrary private/receiver calls, generated factories, Cargo crates and full compiler-input closure require further work; `include!` or rewritten copies are not treated as equivalent original-source proof.

The separate [compiler module-map component](rust-private-callers.md) begins private-caller work with original declaration/body byte locations and resolved parent routes. It supplies no helper injection or private invocation yet, and does not change these public source-build formats.
