# Rust compiler-owned lifetime discovery

<!-- markdownlint-disable MD013 -->

This internal candidate discovers original Rust object types and ownership sites for the [drop bridge](rust-drop-bridge.md). It is the compiler-discovery stage of automatic object/resource lifetime spans. It emits no runtime probes and does not enable public Rust lifetime policies. Developers add no annotations, imports, guards or source edits.

## Compiler boundary

The pinned Rust 1.98.1 compiler request accepts an explicit `lifetime-inspect` mode. It observes `mir_drops_elaborated_and_const_checked`, after borrow checking and drop elaboration, before ordinary optimisation. The provider borrows the fresh body, retains plain metadata and returns the original body unchanged. Layout/type queries run without holding the snapshot lock. The phase is checked as `Runtime(PostCleanup)`, not inferred from output names. The existing function `inspect` and `weave` modes keep their own versioned output and behaviour.

This phase follows the [Rust compiler MIR query model](https://rustc-dev-guide.rust-lang.org/mir/passes.html). The implementation uses the [pinned compiler query](https://github.com/rust-lang/rust/blob/48a229ceaefd4985c50990b14116b6d856af0985/compiler/rustc_middle/src/queries.rs) and [runtime cleanup pipeline](https://github.com/rust-lang/rust/blob/48a229ceaefd4985c50990b14116b6d856af0985/compiler/rustc_mir_transform/src/lib.rs), rather than parsing Rust source text to infer ownership.

The inventory binds source hashes and compiler declaration names/byte ranges. For direct root locals whose resolved ADT declaration belongs to the original application, it retains the actual concrete or generic type, struct/enum/union kind, potential drop-glue requirement, local/argument/return role and target layout. A zero-sized type has size zero. An unavailable generic layout has null size/alignment and an explicit compiler error description; it is not treated as zero-sized or concrete. Target identity and pointer width remain in the record.

Sites describe root-local construction/other assignment, moves, copies, aggregate inputs, calls and drops. Drop cleanup and unwind routes remain explicit. Projected places are labelled as projected root-local operations; they do not claim complete field-level ownership. Const functions, async bodies, closures and expanded bodies are listed as skipped. Constant/static initialisers are outside this profile. Runtime cleanup has already run; the sites describe this MIR view, not every source-level construction. Compiler diagnostics, excessive metadata or changed source bindings refuse publication. Inspection stops before application code generation or execution.

## Common configuration

`otelc.rust_lifetimes.read()` validates the complete inventory before applying the schema-3 TOML policy. Source rules must admit both the operation's function source and the type declaration source. Lifetime include/exclude rules match the compiler-rendered resolved type, including generic arguments; aliases select the underlying resolved type. Exclusions take precedence. Disabled lifetimes select nothing. Function metric/span selectors and live metric switches do not change lifetime discovery selection.

```toml
schema_version = 3
languages = ["rust"]

[sources]
include = ["**"]
exclude = ["vendor/**"]

[lifetimes]
enabled = true
include = ["*Item*", "*Resource*"]
exclude = ["*Internal*"]
max_live = 4096
```

This configuration selects discovery metadata in the internal API. It is not yet an accepted public Rust build/run lifetime policy. The result retains `source_complete=false` and `instrumentation_complete=false`. Reading a report validates its format and original source bindings, not independent proof of which compiler produced it. The build/qualification owner must retain the actual compiler, command and product identities separately. Named resource release methods are not analysed by this drop-discovery profile.

## Checks

`make rust-compiler-check` keeps the genuine existing SDK/function/drop-harness checks and adds the discovery cases and independent 80% floors for the new Rust module and Python helpers. It requires the usual matching tools and at least 8 GiB for fresh builds.

The focused `Rust lifetime discovery` workflow compiles the pinned compiler, runs request tests and Clippy, then executes `make rust-lifetime-check` with explicit `OTELC_RUSTC`, `OTELC_RUST_COMPILER` and `OTELC_RUST_COMPONENT_OUTPUT`. It checks actual aliases, generic/zero-sized layouts, enum/union types, move/copy/drop/call/cleanup sites, source/lifetime exclusion, off state, ordinary function discovery, optimisation requests 0/3 and unbound-input refusals. It requires measured 80% lifetime-module coverage and both Python component floors. It executes compiler processes, not developer applications or an SDK. Existing protected-main required checks are unchanged.

The workflow retains focused source/request/output records, logs, coverage profiles and their compiler executable map for 14 days. Cargo caches, dependency trees and SDK builds are excluded from that artifact. Unresolved failures need their relevant records retained before expiry; a focused workflow is not a frozen shipping provider qualification.

Automatic construction/move/drop insertion, whole-build/dependency and field/container coverage, ownership transfer through calls/returns, zero-sized runtime identity, pinning, async cancellation, public build/launch, actual lifetime overhead and independent oteleq application qualification remain required. Discovery facts alone must not promote any of those capabilities.
