# Object lifetime metrics

## Current opt-in prototype

The product requirement is automatic lifetime instrumentation without application source edits. Selected ordinary [Python collection lifetimes](python-collection.md) now use monitoring and weak references without source edits. Automatic destruction/drop and existing resource close/dispose boundaries remain unimplemented. The [all-language implementation plan](context-and-lifetime-plan.md) explicitly includes **automatic object / resource lifetime spans**, collection boundaries, creation-context links and lifetime metrics. The guard below is an interim opt-in prototype used to exercise runtime lifetime accounting; adding it to a class does not satisfy the source-free acceptance contract.

The installed C++ header [`include/otelc/lifetime.hpp`](../include/otelc/lifetime.hpp) provides an explicit `otelc::ObjectLifetime` guard. The guard measures the interval between `start()` and `finish()` or destruction; it uses a bounded process-wide registry instead of a thread-local function stack. Construction and destruction may occur on different threads.

```cpp
#include "otelc/lifetime.hpp"

class OrderBook {
    // Declared first so the guard is destroyed after later members.
    otelc::ObjectLifetime lifetime_;
public:
    OrderBook() {
        // Construct/initialise the object; any failure may throw normally.
        lifetime_.start("OrderBook"); // Start only after successful setup.
    }
    ~OrderBook() = default;
};
```

Compile with `-Iinclude` and link using `quux-otelc`; configure `[objects] classes = ["OrderBook"]`. Names are exact, case-sensitive class/measurement names, not wildcard patterns. An unlisted name is ignored. The default pool admits up to 4096 live lifetimes; `max_live` is configurable up to 65536 and participates in the memory budget. A full pool increments `object_capacity` loss and does not block the application.

The convenience constructor `ObjectLifetime("name")` begins immediately at the guard's construction. That can intentionally measure setup as well as use. Starting an initially inactive member at the end of its containing constructor avoids recording a failed construction as a successfully created object. Guard placement determines the measured boundary; the implementation does not infer the precise ISO C++ object-lifetime boundary.

## Copy, move, exceptions and identity

Each accepted interval has a unique process-local token. Atomic ownership prevents a reused slot from matching an old token. The same token can complete only once. Raw addresses and tokens are never metric labels or exported object identifiers.

The guard cannot be copied. A containing class that needs copy semantics can define a copy constructor which creates and starts a fresh guard. Moving a guard transfers the existing logical lifetime; the moved-from guard becomes inactive. Move assignment first completes the destination's previous lifetime and then transfers the source's interval. This models a transferred logical resource, rather than two separately timed storage addresses.

The destructor is `noexcept`; ordinary C++ stack unwinding completes an active guard without swallowing or replacing the application's exception. Cross-thread destruction and duplicate-completion races are tested. Constructor failures before `start()` produce no lifetime observation. Process abort, `_Exit`, leaked objects and lifetimes still open at shutdown cannot supply a completed duration; open admitted intervals are counted as `object_incomplete` at shutdown.

## Export and current limits

The worker exports `otelc.object.lifetimes` and `otelc.object.lifetime.duration`, with `code.object.type` and service resource identity. Duration uses the same configured histogram boundaries as function timing. The [Grafana dashboard](observability-stack.md) includes lifetime count and p95 panels.

The C++ guard provides lifetime timing with metrics. Its individual object spans and automatic instrumentation of arbitrary selected C++ classes are not implemented. The explicit guard avoids pretending that trivial/optimised-away constructors, placement-new, inheritance or garbage collection can all be inferred from ordinary function callbacks. Managed languages will need their own resource/lifetime contracts.

## Required automatic mechanism

Configure selected classes externally, then use a language-aware pre-parser and/or compiler adapter to instrument construction/destruction. Generated annotations can retain type and lifetime intent for a downstream compiler pass; existing annotations may guide selection without becoming mandatory. Preserve type information before optimisation and use bounded runtime identity tracking without adding fields to application classes. Validate failed/delegating construction, complete destructor cleanup, implicit/trivial operations, inheritance, placement-new/address reuse and cross-thread destruction. Copy and move construction represent distinct object instances; moving an opt-in guard's logical interval is not the required automatic object model.

An unchanged internal C++ fixture must produce correct lifetime counts and durations without including this header or adding guard members/calls. The [M2 milestone](roadmap.md#m2-selective-llvm-probes-and-reliable-traces) tracks this gap. Rust and managed languages require their own declared initialisation/drop, close/dispose or collection boundaries under the [source-free contract](design.md#source-free-instrumentation-contract).
