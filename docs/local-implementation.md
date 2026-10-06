# Local native implementation

## Current behaviour

The local implementation provides a Rust CLI, strict shared TOML configuration, ELF/Mach-O manifests, optional existing Clang function annotations, live LLVM metrics admission controls, legacy callbacks, a matched LLVM 22 pass for exception-aware C++ timing, an interim opt-in C++ object-lifetime guard, a bounded Rust runtime and an OTLP/HTTP protobuf exporter. Native C and C++ fixtures have run on macOS ARM64. This is a prototype, not a supported release or a completion claim for every M1 platform and release gate.

The runtime archive and CLI are built together. Selected application source is rebuilt with Clang callbacks or the matched LLVM pass, and the runtime is linked into the final executable. Compile-only output has an adjacent `.otelc-object.json` marker containing its digest and function inventory. Link-time wrapping verifies these markers; a raw instrumented object without a marker is rejected. Combined compile/link invocations retain intermediate objects in a temporary directory long enough to identify functions that actually received probes. Temporary files are removed after the build. Uninstrumented inputs and runtime dependencies are not admitted merely because their names match a function rule.

A `.otelc.json` manifest is generated next to the executable before stripping. It records compiler identity, image identity, architecture, function names and selection reasons. Selected symbols with ambiguous names or addresses are rejected. Source file/line extraction is not implemented; no invented source metadata is emitted. `inspect --all` lists excluded symbols, and `inspect --json` returns the retained inventory.

The runtime validates the manifest, relocates addresses under ASLR and allocates fixed per-thread stacks and queues before admission begins. Native TLS caches thread slots and prevents callback re-entry. The shim preserves `errno`. Entry records a monotonic timestamp; a supported normal or exceptional exit publishes one complete observation. A worker owns cumulative count/histogram aggregation. A separate exporter owns bounded HTTP calls and retries. Admission, stack, queue, invalid-exit and incomplete-frame losses are exported independently of the completion queue.

The exporter uses the upstream OpenTelemetry protobuf messages. It supports up to three attempts within one timeout budget for transient errors. Permanent HTTP rejection, malformed responses and partial-success rejection are terminal. Retries can duplicate a batch after an ambiguous transport failure. Normal shutdown stops admission and attempts a bounded drain; fatal termination has no guaranteed flush.

## Build and run

Requirements: Rust 1.98+, Homebrew LLVM 22 (`brew install llvm@22`), and macOS ARM64 for the currently exercised native lane. The local deployment setting is macOS 26.0; the standard library bundled by the tested Homebrew Rust installation has that minimum. Adjusting deployment targets requires a matching runtime/toolchain and native validation.

```sh
make examples
# The four example apps and their manifests are now in build/native.
mkdir -p build/native
./target/debug/quux-otelc --config examples/local.toml doctor
./target/debug/quux-otelc --config examples/local.toml clang -O2 -g \
  tests/fixtures/timing.c -o build/native/timing
./target/debug/quux-otelc inspect build/native/timing
```

Start the Docker Collector/Prometheus/Grafana stack:

```sh
docker compose up -d
```

Then run the executable:

```sh
./target/debug/quux-otelc --config examples/local.toml run ./build/native/timing
./target/debug/quux-otelc --config examples/local.toml run ./build/native/timing threads
```

The first run should report four completions of `selected_recursive`. The threaded run adds forty completions of `selected_work`. `excluded_work` receives callbacks but contributes no timing metric. The application prints `result=3` and preserves its normal exit behaviour.

For the legacy callback backend, an application must already disable C++ exceptions:

```sh
./target/debug/quux-otelc --config examples/local.toml clang++ -O2 -g \
  -fno-exceptions tests/fixtures/timing.cpp -o build/native/timing-cpp
./target/debug/quux-otelc --config examples/local.toml run ./build/native/timing-cpp
```

For exception-enabled C++, select `backend = "llvm"`. The wrapper uses the matched compiler recorded when `make build` builds the plugin; it does not change the system compiler. Try the internal exception and lifetime apps:

```sh
./target/debug/quux-otelc --config examples/exceptions.toml doctor
./target/debug/quux-otelc --config examples/exceptions.toml run ./build/native/exceptions
./target/debug/quux-otelc --config examples/exceptions.toml run ./build/native/objects
```

The LLVM pass selects demangled names before optimisation, retains the probed function inventory, and supplies one invocation token to each normal return or escaping Itanium C++ exception path. Existing catches and cleanup are preserved; unprotected throwing calls get a cleanup landing pad. A catch inside the same function remains a normal completion. The runtime separately counts exceptional exits. Normal fatal-termination behaviour is preserved, without a guaranteed flush.

The lifetime fixture currently includes an explicit guard and therefore does not demonstrate the required source-free object lifetime support. Automatic class instrumentation and the additional language adapters remain TODO under the [source-free contract](design.md#source-free-instrumentation-contract).

See [object lifetimes](object-lifetimes.md), [paired benchmarks](benchmarks.md) and the [Docker metrics viewer](observability-stack.md) for the new workflows. `doctor` checks compiler/runtime/configuration availability; it does not run a compiler fixture or check Collector connectivity.

Start with the [developer 101](developer-101.md) and `examples/config-only.toml` for a shared schema-2 application policy. The legacy schema-1 `examples/otelc.toml` remains accepted. Paths are matched relative to the compiler's working directory. Exclusions win; empty includes supply no configuration opt-ins. Existing Clang annotations may supply optional function opt-ins when enabled with LLVM. Build and run with the same configuration; runtime function rules can narrow the manifest's admitted set but cannot add functions rejected when the manifest was built. For direct execution without the wrapper, explicitly set `OTELC_CONFIG` and optionally `OTELC_MANIFEST`; without `OTELC_CONFIG`, the linked runtime stays disabled.

## Configuration and credentials

Supported environment overrides are `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, generic and metric-specific OTLP endpoints, protocols, timeouts and headers. Signal-specific settings take precedence over generic ones. Base endpoints receive `/v1/metrics`; a metrics endpoint is used as given. Remote endpoints require HTTPS. Endpoint URLs cannot embed credentials. Resource attributes are bounded to 32 entries and reserved service identity is handled separately.

Headers are percent-decoded from `OTEL_EXPORTER_OTLP_HEADERS` or `OTEL_EXPORTER_OTLP_METRICS_HEADERS`, used only by the exporter and never written into manifests or diagnostic messages. Startup failure leaves telemetry disabled with one generic diagnostic. `run` rejects a mismatched manifest before launching the application.

## Validation and remaining qualification

`make check` runs Markdown lint, Python tooling tests, Rust formatting, Clippy, unit tests and native integration tests. `make model-check` exercises the actual ring queue with Loom's tracked cells and atomics. `make rust-coverage` requires cargo-llvm-cov and LLVM tools matching rustc; it includes instrumented Rust code linked into native applications and the C shim, excludes test/vendor sources, and fails below 80% line coverage.

Native tests cover C/C++ selection, recursion, histogram/count correctness, ASLR across repeated processes, threads, admission limits, stack suppression, queue overload, compiler errors, response files, paths containing spaces, separate compile/link, stripping, identity mismatch, exit status and Collector outage. Export tests cover success, transient retry, permanent rejection, malformed responses and partial success. A real local Collector independently decoded the four-call recursive fixture. LLVM fixtures additionally check escaping/recursive exceptions, internal catches, rethrows, cleanup destructors, optimisation levels and threads. Object fixtures check cross-thread destruction, failed construction, moves, capacity exhaustion and slot reuse.

Linux ELF parsing and loader relocation are implemented but have not been qualified on native Linux machines. M1 remains open for Linux x86-64/ARM64 qualification, broad performance/allocation evidence, sanitizer qualification and the release-quality gate. Queue model checking does not prove all thread-lifecycle interleavings. Sampled spans, automatic class lifetime probes, shared libraries, live function-filter changes, CMake integration, async execution and distributed context remain future work. The LLVM lane is qualified locally for Itanium C++ exception handling, not Windows funclets or arbitrary exception personalities.
