# Rust ownership and drop engine candidate

<!-- markdownlint-disable MD013 -->

This internal component extends the existing native OpenTelemetry lifetime engine with `drop_entry` and ownership relocation. It is a building block for automatic Rust object/resource spans. It does not enable Rust lifetime policies: the public Rust build still refuses them until compiler probes, private ABI, SDK bootstrap, health and independent qualification are connected. Developers will not call this API or add guards/imports to their applications.

## Required semantics

Successful value construction starts one observation. Moving ownership transfers its metadata to the new storage without admitting another value, completing the old value, resetting its start time or changing its creation link. The source storage can then hold a new generation. A copy is a separate construction, not a relocation. C++ move construction remains a distinct construction/destruction pair, and the engine refuses relocation in C++ destructor and C allocation/free profiles.

`ObjectRuntime::Relocate` receives only tooling-owned type identity and storage addresses. It does not read, copy, format or modify application storage. Under the engine lock, it transfers the existing map node to a vacant destination and updates the retained address. Self-relocation is a no-op for a live observation. An untracked source remains untracked; capacity refusal does not become a newly invented observation. A destination collision refuses without changing either observation and records duplicate/loss evidence. Invalid inputs, re-entry and moves after close are visible refusals. Operations preserve `errno`.

The first drop entry fixes the end timestamp and claims that generation. Completion of the original drop code reports `otelc.drop.exception_exit`; this is separate from `otelc.destructor.exception_exit`. The lifetime span remains a separate trace linked to the active context at construction. Metric collection/emission gates remain independent of those spans. Objects admitted while collection is off do not gain retroactive metric observations. Shutdown emits `shutdown_censored`, never an invented drop, for observations whose boundary was not reached.

The current metadata key is storage address plus resolved type identity. The compiler must establish valid construction/move/drop coverage before this key can describe Rust ownership. Address reuse alone is not an ownership proof. Zero-sized values, nested selected fields, partial moves, aggregates, opaque container operations and values crossing function boundaries need explicit compiler handling and tests. Do not infer them from this engine component.

## Compiler and public integration still required

1. Inventory resolved types, size/layout, construction sites, moves, copies and drop boundaries through the pinned Rust compiler. Apply the common source and lifetime selectors, including exclusions. Keep unsupported or incomplete coverage visible.
2. Insert private typed hooks after successful construction and at actual ownership transfer/drop paths, including unwind. Do not add fields to application types or change public signatures, traits, ownership, pinning or source bytes. Qualify the MIR phase against optimisation and drop elaboration so elided storage or cleanup cannot silently remove observations.
3. Connect the compiler bridge and explicit native SDK plan to the drop engine. Retain versioned build/selection/health records, bounded metadata, censored/lost observations and actual SDK drain. Existing C/C++ ABI and observation boundaries must keep their meanings.
4. Exercise local, returned, argument, nested and container moves; copies, address reuse and zero-sized values; ordinary/failed construction and panic/drop cleanup; cross-thread/task ownership, cancellation, pinning and `mem::forget`. Unobserved release must remain censored rather than becoming a fabricated destructor.
5. Qualify unchanged original applications through oteleq, all four startup/live metric states and decoded OTLP. Retain actual stock/instrumented/lifecycle overhead comparisons and Grafana receipt. Complete the required 80% component coverage, review and protected main checks before claiming language support.

## Component checks

The native object-engine tests now include ownership relocation, original timing/creation identity, source-address reuse, cross-thread relocation, destination collision/refusal, drop/unwind labels, all metric startup states, later enabling and shutdown censoring. The existing errno/re-entry matrix includes the drop boundary. These are real SDK engine tests when executed through `make native-check`; they are not automatic Rust compiler tests.

Local syntax checks alone do not establish that behaviour or its coverage. Fresh SDK/compiler builds require 8 GiB free. Use the hosted exact-head checks when the local storage floor is not met, and retain failed attempts and their actual revision identities.
