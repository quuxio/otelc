# Rust drop bridge and SDK candidate

<!-- markdownlint-disable MD013 -->

This internal component connects the [ownership/drop engine](rust-drop-engine.md) to private Rust hooks, an explicit SDK plan and versioned health. Developers do not call these hooks. Automatic compiler insertion into unchanged Rust applications and public lifetime configuration remain required before Rust lifetime support can be enabled.

## Observation contract

The tooling-owned `compiler_mir` bridge supplies `drop_created`, `drop_moved`, `drop_enter`, `drop_exit` and `drop_unwind`. Construction and ownership transfer carry opaque storage identities; the hooks never dereference or retain application values. A move transfers the existing observation without resetting its clock, generation or creation link. Completion uses the original normal or unwind path and does not inspect the panic payload. `mem::forget` without a later observed drop produces shutdown censoring.

The `drop_new(type_usr, name)` hook supplies a process-wide non-zero observation key for compiler-owned lifetime insertion. The key is independent of an object's address and size: moving the value transfers the same key, simultaneous zero-sized values receive distinct keys, and forgotten values cannot collide with later stack reuse. Keys never wrap or recycle. Exhaustion or malformed metadata returns zero and increments the existing metadata-error counter. A non-zero key identifies the observation attempt; it does not promise SDK admission. `drop_enter(key, type_usr)` still returns the actual admission token. Key-based and storage-based identities are separate internal producer conventions and must not be mixed for the same type within an application instance.

The compiler supplies bounded UTF-8 identities with one trailing NUL and no interior NUL. The bridge checks borrowed bytes without allocation or a panic. Invalid identities increment the SDK metadata-error counter and create no observation. Relocation through another lifetime profile refuses visibly. All exported boundaries preserve the caller's `errno`.

The private native plan is version 3, language `rust`, with `settings.drop_lifetimes = {enabled: true, max_live: N}`. The positive live limit is at most 1,000,000. Object, resource and drop profiles cannot be combined, including disabled entries. Function-only Rust keeps plan version 2. Existing C/C++ plan and ABI meanings remain unchanged.

Drop health is version 5 and requires Rust `drop_accounting`, with `dropping` for claimed drop callbacks. It distinguishes admitted, completed, live, censored, lost, duplicate and invalid observations. Version 1 cannot claim drop accounting; version 5 cannot claim C++ destruction or C free accounting. Function, exporter and SDK drain evidence remains separate from downstream receipt. `source_complete=false` remains explicit.

## Checks and limits

On a host with at least 8 GiB free and the pinned dependencies, run:

```sh
make native-launch-check
make rust-compiler-check
```

The first command builds the genuine SDK and exercises a tool-owned ABI harness, strict plan refusals, preserved `errno`, all four startup states and live metric switches. The second builds the matching Rust bridge/compiler and explicitly labelled private Rust harnesses. The drop harness observes actual ownership transfer into heap storage, normal drop, a caught panic in drop, forgotten allocations, malformed metadata and unchanged application side-effect counts. The key harness distinguishes simultaneous zero-sized values, transfers one observation through a move and keeps a forgotten predecessor distinct from later values. Three compiled unit tests cover monotonic allocation, permanent exhaustion and concurrent key uniqueness. Both harnesses require real SDK health and decoded OTLP. The original eight synchronous Rust application launches remain required; four private drop-harness and four private key-harness launches are recorded separately. Rust bridge coverage includes the original, both harness and key-unit executable maps and their actual profiles, so unlinked hooks cannot disappear from the coverage denominator. Every existing 80% component floor remains enabled.

These explicit harnesses do not qualify automatic lifetime instrumentation. Resolved-type inventory and selection, construction/move/drop MIR insertion, layout and zero-sized values, nested and partial moves, containers, arguments/returns, pinning, async cancellation, independent oteleq application qualification and lifetime benchmarks remain work. The public Rust policy must continue to refuse lifetimes until those criteria pass. Local syntax, parser and literal-report tests alone do not establish runtime behaviour or SDK/Rust coverage.
