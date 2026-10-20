# Experimental Rust compiler hooks

The explicit `llvm` backend instruments unchanged standalone Rust source after parsing and macro expansion. It remains under qualification and does not replace the default generated-source backend. The default backend's source-column limitation and issue #66 remain open. Async completion, Cargo integration, automatic lifetimes and diagnostic invocation scopes are unavailable in this compiler profile.

## Build the pinned profile

The qualified local pairing is macOS ARM64, rustc `1.101.0-nightly` from `nightly-2026-09-28`, full commit `d080e7dff1b0fc54541545252818f8cccf995d05` (LLVM 23.1.1), and a pass built with LLVM 23.1.3. Other nightlies, hosts or pass versions are rejected.

```sh
rustup toolchain install nightly-2026-09-28 --profile minimal --component rustc-dev --component clippy
brew install llvm@23
OTELC_RUSTC="$(rustup which --toolchain nightly-2026-09-28 rustc)" make rust-llvm-build
```

The ordinary CLI build still uses Rust 1.98.1. The builder creates a separate nightly SDK, compiler frontend and LLVM pass. Profile schema 2 records their SHA-256 fingerprints, the actual compiler and protected SDK source roots in `target/debug/otelc-rust-llvm.json`. The frontend uses the pinned compiler's own analysis and monomorphisation records; its published binary is `target/debug/otelc-rust-frontend`. Rebuild older profiles before using this adapter. The builder refuses unexpected compiler wrappers, Rust flags and cross-target settings. The installed LLVM must report exactly 23.1.3; installing an unqualified version does not enable the backend. No global Rust default is changed.

## Source and common configuration

The [example source](../examples/apps/rust_compiler_locations.rs) contains ordinary Rust, including `line!`, `column!` and a tracked caller. It has no probes or SDK imports. Its [policy](../examples/rust-compiler.toml) selects the function externally:

```toml
schema_version = 2
languages = ["rust"]
[adapters.rust]
backend = "llvm"
[sources]
include = ["examples/apps/rust_compiler_locations.rs"]
[functions]
include = ["*.selected"]
[traces]
enabled = true
root_sample_ratio = 1.0
```

Existing annotations use the same original-source parser and exclusion rules as the default backend. Set `annotations.read_existing=true` to read them. No annotation is required, and the compiler backend never inserts text into application or dependency sources.

The [macro and generic example](../examples/apps/rust_compiler_expanded.rs) also needs no annotations. Its [policy](../examples/rust-compiler-expanded.toml) selects `*.generated` and `*.generic`. The compiler supplies the generated declaration and the two concrete generic symbols. Generic instances retain one configured function name while their separate symbols remain in the inventory. Ordinary declarations keep their existing configured names and annotations. For generated declarations without an unambiguous original alias, names combine the entry path with compiler definition components; source selection uses the original macro callsite. Comments on a macro invocation are not treated as annotations on every expansion.

```sh
make stack-up
./target/debug/quux-otelc --config examples/rust-compiler.toml rust --doctor
./target/debug/quux-otelc --config examples/rust-compiler.toml rust \
  --build examples/apps/rust_compiler_locations.rs --output-dir build/rust-compiler
./target/debug/quux-otelc --config examples/rust-compiler.toml rust --run build/rust-compiler

./target/debug/quux-otelc --config examples/rust-compiler-expanded.toml rust \
  --build examples/apps/rust_compiler_expanded.rs --output-dir build/rust-compiler-expanded
./target/debug/quux-otelc --config examples/rust-compiler-expanded.toml rust --run build/rust-compiler-expanded
```

Build-only does not execute the application. The output directory must not already exist. `--run` needs the matching adapter and bundle; it needs neither compiler, original source nor matching SDK installation. Runtime policy can change; build-time selectors, backend and annotation settings must still match.

## Baselines and provenance

Use the actual compiler and `baseline_flags` recorded in `build.json` for the ordinary build. This profile records `--edition=2024 -O -g -Zmir-enable-passes=-Inline,-ForceInline -Ccodegen-units=1`. Disabling MIR inlining retains selected bodies until LLVM instrumentation; a baseline with different optimisation settings would misstate runtime overhead.

LLVM bundles use build schema 2 and `backend="llvm"`. Legacy source bundles retain schema 1. In addition to executable/plan/adapter/SDK fingerprints, the compiler bundle retains:

- `profile.json` (schema 2): the exact compiler/SDK/pass/frontend descriptor.
- `inventory.json` (schema 3): original input hashes, declaration byte coordinates, resolved common selection, the dependency-discovery command, compiler definition names, expansion flags and concrete emitted symbols. Uninstantiated declarations have `emitted=false` and an empty symbol; they cannot establish runtime coverage.
- `resolved.json` (schema 1): actual symbols matched and instrumented at `llvm-pipeline-start`, with original names, source hashes, body coordinates and selected/startup flags.

All three records are fingerprinted and checked on launch. They bind build provenance; they do not prove that a particular runtime invocation completed. Raw spans carry function names without a diagnostic nonce/probe-ID API. Consumers must not credit a startup call to a later tested invocation.

The driver asks the original compiler for dependency information and parses actual inputs, rather than unrelated files elsewhere in the project. The frontend then binds compiler-owned local definitions to exact emitted LLVM symbols, including macro-generated functions and generic instances. Every selected emitted symbol must resolve; a missing body fails the build. Original aliases retain annotations and byte coordinates, including declarations loaded with `include!`. Const evaluation finishes before instrumentation, so only emitted runtime calls to const functions produce telemetry. Cfg-disabled async declarations do not block a build; selected live async bodies are refused before application execution.

Input hashes are checked against the compiler's loaded source and around compilation. Environment-dependency comments are discarded because `env!` values can contain application data. Dependency discovery and the frontend add build-time work. Include expression fragments, arbitrary procedural-macro behaviour and concurrent source mutation remain unqualified.

## Evidence and remaining gates

Fifteen compiler tests use the real pinned SDK/pass/frontend, compare raw baseline/application channels and decode OTLP spans. They cover direct/qualified/aliased/forwarded location macros, tracked callers, inline bodies, untouched annotations, nested same-line functions/closures/specialised methods, Unicode, CRLF, module names containing spaces/`#`/`$`, caught and escaping panics, selected-main cleanup, worker-thread isolation, admission recovery, isolated replay, generated functions/methods/closures, generic instances, included declarations, const runtime versus CTFE calls and explicit async/Cargo refusal. Relative and absolute entry paths give identical generated method names. Worker functions produce independent thread roots; this does not propagate a creator's context automatically. Output equality alone cannot pass a missing-probe case.

After building the pinned profile, `make rust-llvm-coverage` rebuilds the adapter, SDK, frontend and pass with LLVM 23 coverage and runs those tests explicitly. It also checks frontend formatting and Clippy with the matching nightly. `make rust-coverage` and `make check` include this qualification alongside the stable LLVM 22 tests. Install and build the pinned profile first; CI performs that setup automatically. Compiler inputs, profile, driver, frontend protocol, compiler frontend and LLVM pass each have their own 80% floor. Raw profiles stay in separate toolchain directories; only exported source-line hits are combined, with source and build-script hashes checked before and after qualification. A concurrent run in the same compiler-coverage directory is refused.

A missing-probe regression exposed character-based parser columns being compared with byte-based compiler debug columns. Another baseline produced only two generic calls while silently missing the selected macro-generated function. Compiler-owned identities now replace debug-location heuristics for symbol binding. Ambiguous original aliases fail instead of silently skipping instrumentation. A crate-attribute macro experiment was rejected because it collapsed tested locations to line/column 1.

A separate real worker fixture originally failed to build because declaration matching tried to resolve unselected thread closures. Exact emitted-symbol binding keeps those unselected bodies outside instrumentation. The regression checks four worker trees, recovery after a caught panic and the original exception payload.

The earlier three-function CLI example stored three spans and one complete tree in Tempo with unchanged source/output and zero loss. This is scoped local evidence, not broad semantic equivalence or production-overhead qualification.

The `--rust-llvm` language benchmark builds the plain lane from the recorded compiler/flags and replays one instrumented bundle through live metrics-off/on phases. Local function and synchronous-closure runs preserve source/checksums and report exact measured-call totals with zero loss. These runs exercised the mechanism during concurrent validation and are not quiet-host performance qualification.

Before merge, complete the integrated compiler-profile CI/LLVM coverage gate, independent consumer qualification and final integration review. Foreign exception unwinding, panic-abort telemetry and thread-local destructor shutdown are not qualified. Every new product component must meet its own 80% floor. This implementation remains a draft until those gates pass.
