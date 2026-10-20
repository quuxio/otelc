# Rust compiler-owned probe development

This is the development path for replacing generated Rust body guards with compiler-owned instrumentation. The public `compiler` adapter now uses this helper for standalone sources and Cargo launch. Source-column issue [#66](https://github.com/quuxio/otelc/issues/66) remains open until the real CLI, Cargo wrapper, retained builds, coverage and independent oteleq qualification use and verify the replacement. The separate experimental `llvm` backend retains its existing profile and support limits.

## Build and run the qualification

The helper requires macOS ARM64, Rust 1.98.1 commit `48a229ceaefd4985c50990b14116b6d856af0985`, LLVM 22.1.8 and that toolchain's `rustc-dev` component. It uses private compiler APIs; a different patch release is not interchangeable. Start with the standard [local workflow prerequisites](../README.md#local-workflow), including LLVM/Clang and the supported Node version. Install the additional compiler component once, then run from the repository root:

```sh
rustup component add --toolchain 1.98.1 rustc-dev
rustup run 1.98.1 make rust-mir-check
```

`rust-mir-build` builds the ordinary workspace products, compiles the private helper and writes `target/debug/otelc-rust-compiler.json`. The descriptor records the actual stable compiler, helper and SDK paths and SHA-256 fingerprints. It is published after the build succeeds. `OTELC_RUSTC` continues to select the optional LLVM launch profile and does not select this helper's build compiler.

Bootstrap permission is restricted to the `otelc_rust_mir` crate while building and checking the helper. The qualification launcher removes it before compiling applications, and the helper rejects inherited `RUSTC_BOOTSTRAP`. An application with an unstable feature still receives stable Rust's `E0554` error.

`rust-mir-check` runs the maintained tests in `native/rust/mir/tests/compiler.rs`, then strict Clippy across the compiler and its tests. These tests construct bounded compiler requests from the common resolved policy and the original declaration inventory. Each positive pair uses the same original fixture and compares raw stdout, stderr and exit status, checks every source hash, and decodes real SDK OTLP messages. A count alone cannot pass the synchronous identity check: the expected original function names must also appear. Compiler and application processes have explicit deadlines.

## Boundaries exercised

The unchanged fixtures cover original `column!()` and tracked callers, an external module, a macro-generated generic function, a loop and caught synchronous panic. Async fixtures check first-poll admission, repeated pending polls, cancellation across threads, unpolled futures, original future size/alignment, `needs_drop`, `Send` and application drop counts. An escaping async panic preserves its payload, completes one error span and leaves the next independent poll with clean context. Original-source cleanup pairs also distinguish cancellation during an unrelated caller panic from an actual nested synchronous, async or destructor panic. Compiler return and unwind edges carry explicit outcomes; they do not infer a body panic from the thread's ambient panic state.

The original declaration inventory reads existing `otelc.instrument` comments. Config-only selection and existing comments use the same exclusion precedence. Macro-owned definitions still use compiler identities where no original declaration alias exists.

Future metadata lives in a bounded external registry keyed by a pinned frame identity and compiler site. It contains no application reference or owning runtime guard, and no field is added to an application's future. Hard identity-capacity exhaustion rejects unknown future identities for the rest of that process; known entries can still complete. This preserves rejection decisions rather than resampling an unremembered suspended future after capacity is freed. Losses and the exhausted state are reported. Pending work at shutdown is censored rather than completed artificially.

## Allocator safety

SDK operations, exporter/control workers and compiler-identified standard `GlobalAlloc` methods suppress recording while they execute. Recording itself allocates; doing so recursively inside an allocator, particularly one holding its own lock, can overflow the stack or deadlock. The [pinned standard-library contract](https://github.com/rust-lang/rust/blob/48a229ceaefd4985c50990b14116b6d856af0985/library/core/src/alloc/global.rs#L117-L131) describes this re-entry constraint.

Allocator methods and helpers called within their dynamic scope do not produce function observations, even when selected. The compiler resolves the actual core trait, so an unrelated application trait or method with the same spelling remains eligible. The core crate is identified through the compiler language item, so an application named `core` cannot impersonate it. Safety also applies to a local allocator implemented in an external `#[path]` module outside the selected project root. This does not qualify an allocator supplied solely by an independently compiled dependency.

The SDK suppression guard is thread-local, stack-only and restored on ordinary return or unwind. It is not held across a future suspension. The maintained tests include a lock-based allocator and a lookalike trait; retained development reproductions also cover the earlier exporter stack overflow and the external-module deadline failure.

## Fresh compiler and SDK coverage

```sh
rustup run 1.98.1 make rust-mir-coverage
```

The maintained gate uses the pinned stable compiler and LLVM 22.1.8 tools. It builds an instrumented SDK, runs its unit tests, builds a separately instrumented compiler helper and executes all eight positive original-source compiler pairs plus the negative compiler tests. Each executable has its own fresh raw-profile directory and retained map; exports use `--fatal-warnings` before source-line hits are combined. SDK tests and vendor sources are excluded from product coverage. Compiler entry, hook construction, MIR insertion, compiled inventory, shared definitions, SDK future observers, recording suppression, completion and context each require at least 80% line coverage.

Evidence stays under `target/rust-mir-coverage/run-*`: build/test output, exact executable maps, raw profiles, retained plain/instrumented application output, original fixture sources, policy/request and runtime reports. `build/rust-mir-coverage.json` binds the final report to source hashes and that run. Missing profiles, a changed map, incomplete compiler pairs, LLVM diagnostics, a component below its floor or source changes prevent publication. A new attempt retains previous summary files with its evidence; concurrent qualification cannot delete another run's data. Ordinary and frozen products are not overwritten.

`make rust-coverage` also runs this gate, reusing its existing instrumented SDK cache and merging only the exported source-line hits with the stable/native and LLVM 23 reports. CI installs stable `rustc-dev` for this step. The earlier eight-component maintained gate passed on 20 October 2026 with seven compiler tests, eight positive application pairs and no ignored tests. Measured line coverage was 93.43% for the compiler entry, 96.49% for hook construction, 97.28% for MIR insertion, 82.25% for shared definitions, 83.16% for SDK future observers, 100% for recording suppression, 93.39% for SDK completion and 100% for context. These measurements qualify the earlier private compiler gate; fresh measurements are required for the public routing change and its added compiler inventory component.

## Remaining delivery gates

Earlier development qualification at `8917c90` measured each compiler-helper file above 80% line coverage and the compiler SDK hooks at 82.61%, using fresh real-process profiles. The explicit panic-outcome changes now have fresh maintained-gate evidence above. The public adapter routes original standalone and Cargo inputs through the helper, and schema-3 bundles retain separate driver/request/inventory provenance for detached replay. Independent oteleq qualification and final full-project checks remain outstanding. The root coverage gate now includes the maintained compiler reports; its complete execution is a separate delivery check. SDK unit tests and private compiler pairs do not establish those behaviours. Async executor/task propagation and automatic typed object/resource lifetime spans are separate work; observing an async function's completion is not a typed object lifetime.
