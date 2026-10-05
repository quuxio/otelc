# Runtime design

## State and initialization

The runtime has `disabled`, `initializing`, `running`, and `draining` states. It starts disabled. Initialization reads and validates configuration, resolves a manifest against loaded image identity, allocates fixed pools, establishes clock anchors, and starts one worker. Only a completely initialized runtime transitions to running.

Failure leaves probes disabled and emits at most one bounded diagnostic. Constructors that run before initialization and teardown after draining are not observed. Explicitly documenting this interval is preferable to attempting loader work, allocations, or thread startup from the first instrumented function.

## Per-thread ownership

Each admitted application thread owns a fixed stack and a single-producer/single-consumer completion queue. Thread registration claims a preallocated slot during a cold first-use path. TLS initialization and thread registration costs are measured separately from steady state; the design does not assume that the operating system's first TLS access is allocation-free.

There are at most `max_threads` slots. Exhausting them suppresses that thread's probes and increments an admission-loss counter. Thread exit marks its slot retired. The worker drains the queue, discards incomplete frames, then returns the slot to the pool with a new generation. TLS teardown never waits for export.

## Entry and exit

Entry checks the global running state, the thread suppression/reentrancy guard, function admission, and stack capacity. A selected invocation pushes a frame with its function key, monotonic start, invocation token, configuration generation, and nearest selected parent's token. Unselected functions do not become synthetic parents.

For a valid normal exit, the owning thread reads monotonic time, calculates inclusive elapsed duration, and publishes a complete record. Computing duration before publication removes the need to reconstruct an entry/exit pair from a lossy queue. Recursion uses a separate frame for each invocation.

A queue-full condition drops the complete record and increments a per-thread loss counter. It does not leave an entry in the worker waiting forever for an exit. Callback address mismatches discard intervening frames and invalidate nesting. The legacy backend has known limits detecting exceptional recursive unwinds and is therefore restricted to normal-return execution.

## Fixed completion record

The proposed internal record is 64 bytes and is checked by target-specific layout tests:

| Field | Size | Meaning |
| --- | --- | --- |
| `function_key` | 8 bytes | Process-local function identity |
| `invocation` | 8 bytes | Accepted invocation token |
| `parent_invocation` | 8 bytes | Nearest admitted parent or zero |
| `trace_root` | 8 bytes | Root token, or zero when tracing is off |
| `start_tick` | 8 bytes | Monotonic start |
| `duration_tick` | 8 bytes | Inclusive monotonic duration |
| `thread_slot` | 4 bytes | Owner slot |
| `config_generation` | 4 bytes | Policy pinned at entry |
| `flags` | 4 bytes | Outcome and supported trace flags |
| `reserved` | 4 bytes | Reserved, zero |

This is an internal structure, not an OTLP message or persistent file format. Platform clock ticks are converted to nanoseconds outside the producer path where possible.

## Publication and overload

The producer writes a free queue slot and release-publishes its sequence. The worker acquire-loads that sequence before consuming it, then releases ownership back to the producer. Producer and consumer cursors are separated to avoid avoidable cache contention. Sequence arithmetic, wrap, retirement, and reuse need deterministic tests plus a Rust concurrency model check such as Loom.

Queue loss, stack-depth overflow, unknown functions, thread admission failure, and invalid exits are separate counters. The worker reads producer loss counters independently of the completion queue so a full queue cannot hide its own loss. Counters saturate at their integer maximum rather than wrap silently.

When a stack overflows, a suppression depth tracks the unsupported subtree while existing frames remain intact. The token backend can return zero for rejected descendants. The legacy backend must still track entry/exit suppression without writing beyond capacity; unsupported missing exits invalidate that contract. No implementation may claim overflow correctness solely because its normal recursion test passed.

## Memory budget

The draft defaults are 128 thread slots, 256 frames per thread, and 4096 completion records per thread. At a target frame size of 64 bytes and record size of 64 bytes, those pools reserve approximately 34 MiB. The function registry, cumulative histograms, worker batches, and optional trace buffers have separate fixed limits. `doctor` and `inspect` must report the calculated total budget before running.

The initial implementation preallocates the configured budget. Lower-memory profiles can select smaller limits; the runtime never grows past them under load. Function admission is capped at 4096 selected functions by default and fails initialization if configuration demands more than the bound.

## Worker and exporter

The worker drains queues fairly, aggregates completions, reads loss counters, and emits export batches on an interval or batch-size threshold. An exporter has a bounded queue and bounded retry time. If the backend is unavailable, batches are discarded with an export-loss count. Application threads remain independent of the backend's availability.

OTLP dependency objects, clocks, histogram maps, serialization, compression, TLS, and HTTP clients are owned by worker/exporter paths. A separate worker avoids re-entering the hot path through OpenTelemetry internals. These runtime threads are always suppressed.

## Clocks, shutdown, and process changes

Durations use a monotonic platform clock; they cannot become negative when wall time changes. The worker periodically refreshes the monotonic/realtime mapping for OTLP timestamps. Each trace pins an anchor generation so one trace cannot jump between anchors. Exact clock conversion and suspend behaviour are platform tests.

Normal shutdown stops new admission, drains completed records, and attempts export within the configured deadline. A still-running application thread's open frames are incomplete and are discarded with diagnostics. Never synthesize successful durations for them. `abort`, `_exit`, fatal signals, and forced termination do not promise a flush.

In a fork child, inherited worker threads and synchronization state cannot be reused. A platform adapter must disable telemetry in the child without blocking; reinitialization is a later capability. `exec` starts a fresh runtime. Until the fork fixture passes, fork while instrumented is outside support.

Signal handlers, thread cancellation, dynamic module unloading, async tasks, fibers, and context switching outside ordinary native threads are explicitly unsupported in the initial runtime. See the [support matrix](support.md).
