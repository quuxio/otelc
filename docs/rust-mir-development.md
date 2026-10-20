# Rust compiler-owned probe development

This is the development path for replacing generated Rust body guards with compiler-owned instrumentation. It does not change the public `compiler` adapter yet. Source-column issue [#66](https://github.com/quuxio/otelc/issues/66) remains open until the real CLI, Cargo wrapper, retained builds, coverage and independent oteleq qualification use and verify the replacement. The separate experimental `llvm` backend retains its existing profile and support limits.

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

The unchanged fixtures cover original `column!()` and tracked callers, an external module, a macro-generated generic function, a loop and caught synchronous panic. Async fixtures check first-poll admission, repeated pending polls, cancellation across threads, unpolled futures, original future size/alignment, `needs_drop`, `Send` and application drop counts. An escaping async panic preserves its payload, completes one error span and leaves the next independent poll with clean context.

The original declaration inventory reads existing `otelc.instrument` comments. Config-only selection and existing comments use the same exclusion precedence. Macro-owned definitions still use compiler identities where no original declaration alias exists.

Future metadata lives in a bounded external registry keyed by a pinned frame identity and compiler site. It contains no application reference or owning runtime guard, and no field is added to an application's future. Hard identity-capacity exhaustion rejects unknown future identities for the rest of that process; known entries can still complete. This preserves rejection decisions rather than resampling an unremembered suspended future after capacity is freed. Losses and the exhausted state are reported. Pending work at shutdown is censored rather than completed artificially.

## Allocator safety

SDK operations, exporter/control workers and compiler-identified standard `GlobalAlloc` methods suppress recording while they execute. Recording itself allocates; doing so recursively inside an allocator, particularly one holding its own lock, can overflow the stack or deadlock. The [pinned standard-library contract](https://github.com/rust-lang/rust/blob/48a229ceaefd4985c50990b14116b6d856af0985/library/core/src/alloc/global.rs#L117-L131) describes this re-entry constraint.

Allocator methods and helpers called within their dynamic scope do not produce function observations, even when selected. The compiler resolves the actual core trait, so an unrelated application trait or method with the same spelling remains eligible. Safety also applies to a local allocator implemented in an external `#[path]` module outside the selected project root. This does not qualify an allocator supplied solely by an independently compiled dependency.

The SDK suppression guard is thread-local, stack-only and restored on ordinary return or unwind. It is not held across a future suspension. The maintained tests include a lock-based allocator and a lookalike trait; retained development reproductions also cover the earlier exporter stack overflow and the external-module deadline failure.

## Remaining delivery gates

Local qualification has measured each new compiler-helper file above 80% line coverage and the compiler SDK hooks at 82.61%, using fresh real-process profiles. Public CLI and Cargo routing, retained-artifact provenance and replay, automated integration of those coverage reports, independent oteleq qualification and final full-project checks remain outstanding. SDK unit tests and private compiler pairs do not establish those behaviours. Async executor/task propagation and automatic typed object/resource lifetime spans are separate work; observing an async function's completion is not a typed object lifetime.
