# Rust automatic lifetime compiler component

<!-- markdownlint-disable MD013 -->

This candidate inserts construction, whole-local ownership move and drop observations into compiler-owned Rust MIR. Application files contain no telemetry imports, annotations, hooks or guards. It builds on the [resolved lifetime inventory](rust-lifetime-discovery.md) and [SDK bridge](rust-drop-bridge.md). Genuine compiler/SDK execution, measured coverage, independent review and protected delivery are required before claiming this component verified. Public Rust lifetime policies remain disabled while broader ownership and language qualification are unfinished.

## Common policy and compiler bindings

`otelc.rust_lifetimes.weave_bindings` reads the original compiler inventory, rechecks source hashes and applies the same schema-3 source and lifetime selectors. It retains every selected local, including unsupported shapes; it does not silently discard an argument or unknown layout. The resulting function/local/type bindings go into a separate internal compiler request: `schema_version = 2`, `mode = "lifetime-weave"`, with a non-empty `lifetimes` map. Existing schema-1 inspection, function weaving and lifetime inspection remain separate.

The compiler checks those bindings against its own original source, resolved ADT, drop requirement and concrete layout. It weaves the runtime MIR immediately after drop elaboration and before ordinary optimisation. A successful aggregate or constant materialisation calls `drop_new`. A whole-local move transfers its observation key without starting another lifetime or changing the application move. Keys distinguish simultaneous zero-sized values and reused storage without retaining application objects.

The original drop calls, drop flags and cleanup edges remain in place. The probe claims a lifetime immediately before the actual drop, completes it on the normal successor and marks an exception only on that drop's unwind successor. A normal destructor that runs during cleanup of an earlier panic is therefore a normal drop observation. Double-panic termination and abort paths retain their original behaviour; they do not become successful observations.

## Focused acceptance checks

On a host with the pinned toolchain and at least 8 GiB free, run:

```sh
make rust-compiler-check
```

The component workflow runs this command on Linux and macOS. It requires the original synchronous application corpus and explicit private bridge/key harnesses, plus an unchanged-source automatic lifetime application built at optimisation levels 0 and 3. The automatic application has ordinary `Item` and zero-sized `Zero` types with real `Drop` side effects, an ownership move, normal return, a panic in `Drop` and an outer panic with normal cleanup. Stock and instrumented results, drop counts and panic output are compared. Every startup collection/emission combination then switches live in the same PID. Decoded OTLP must show twelve completed lifetimes for four invocations, exactly one exceptional drop, correct function links and no lifetime loss, duplication, censoring or invalid metadata.

Changed bindings, missing locals, non-drop types, unresolved generic layouts, by-value arguments, factory returns and ownership escapes must refuse compilation without an executable or successful inventory. The new compiler module, Python binding/helper code and all existing components retain separate 80% coverage floors. Hosted artifacts retain small original diagnostics, application sources, health, decoded telemetry and measured reports for 14 days; compiler/SDK build trees are excluded.

## Remaining ownership support

This first insertion component handles selected root-local values with a concrete layout and an actual drop boundary. By-value arguments and returns, factory/call ownership transfer, `mem::drop`/`mem::forget` calls, projected and partial moves/drops, containers, const/async/closure/macro bodies, non-drop storage lifetimes and arbitrary Cargo builds need further compiler support. Unsupported selected operations fail visibly. The component does not establish complete source/dependency closure, public lifetime launch support, independent oteleq admission, lifetime overhead comparisons or Grafana receipt.
