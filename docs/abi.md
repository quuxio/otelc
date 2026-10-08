# Probe ABI proposal

## Status

This is a draft contract for the LLVM backend. The local runtime implements legacy callbacks and an interim address-based token ABI (`otelc_function_enter_v1` and `otelc_function_leave_v1`) for the LLVM timing pass, plus `otelc_object_begin_v1`/`otelc_object_end_v1` for the installed C++ lifetime guard. The descriptor-based token interface below remains a proposal. The callback prototype exposes the compiler's legacy ABI and adapts it internally; it does not pretend that Clang already passes these descriptors or tokens.

## Versioned interface

The proposed native interface uses C linkage, fixed-width integer fields, naturally aligned native pointers, and ABI-versioned symbol names. It is an in-process ABI, not a portable serialized file layout.

```c
#include <stdint.h>

typedef struct otelc_function_v1 {
    uint32_t struct_size;
    uint16_t abi_version;
    uint16_t flags;
    uint64_t local_id;
    const char *linkage_name;
    const char *display_name;
    const char *file_path;
    uint32_t line_number;
    uint32_t reserved;
} otelc_function_v1;

enum otelc_exit_v1 {
    OTELC_EXIT_RETURN = 0,
    OTELC_EXIT_UNWIND = 1
};

uint64_t otelc_enter_v1(const otelc_function_v1 *function);
void otelc_leave_v1(uint64_t token, uint32_t exit_kind);
```

A descriptor is immutable and valid for the registered module's lifetime. `struct_size` and `abi_version` allow the runtime to reject incompatible descriptors during module registration. `local_id` is unique only within that module; the runtime assigns a dense, process-local function key after checking the module identity. File paths are normalized before registration. Missing metadata uses null pointers and line number zero.

Future descriptor changes that alter field interpretation require a new ABI version. Serialized manifests carry their own schema version and must not dump native structures containing pointers.

## Invocation tokens

`otelc_enter_v1` returns zero when the invocation is rejected or the runtime is disabled. `otelc_leave_v1(0, ...)` is a no-op. A nonzero token identifies one accepted invocation within one thread and slot generation; callers treat it as opaque and pass it back exactly once on a supported exit.

The token permits detection of an out-of-order exit even when a function recursively calls itself. It does not represent a trace ID, span ID, memory address, or user-controlled object. Tokens must not be transferred between threads or reused after a leave. Counter/generation wrap must disable further admission before a token could alias a live invocation.

The pass emits cleanup so every accepted invocation gets a leave along each supported exit. Invalid tokens cause bounded loss accounting and a frame reset, not a panic across the C boundary. A leave never reads application arguments or return values.

The implemented LLVM token ABI also returns an opaque nonzero suppression token for a rejected traced scope. The compiler must pass it to the matching leave; it records no function observation and prevents descendants from becoming false roots while an outer scope is rejected. This is distinct from the proposed descriptor ABI. Never interpret a nonzero interim token as proof that telemetry was recorded.

## Lifecycle

Native module initialization registers descriptors and a module build identity before those probes are admitted. Registration may allocate; function probes may not. The runtime copies or owns all metadata needed after registration. Modules with unsupported unload behaviour remain excluded until the module-lifetime contract is implemented.

A small native initializer starts the runtime only after configuration and manifest validation. Before initialization and during shutdown, probes return zero. Native initialisation and the shutdown callback suppress probes on the calling thread. Aggregation, exporter and control threads start through a C entry that sets thread-local suppression before entering Rust or running telemetry work. This also excludes calls to an application-provided allocator from worker startup; setting suppression only inside a Rust thread closure is too late. Workers have a 2 MiB stack and retain their operating-system thread names. Application threads still record calls to selected allocators, and shim callbacks preserve `errno`. The macOS zone-allocator integration fixture checks both native backends; interposed allocators on other targets remain unqualified.

The eventual manual context/flush APIs will be separately versioned. Do not extend this minimal interface with unused error, argument, or distributed-context hooks before an implemented adapter needs them.

## Safety obligations

- No panic, C++ exception, or foreign unwind may cross a probe boundary.
- Runtime code must be linked once and built without its own probes.
- The shim preserves application-visible `errno` around probe work.
- No global allocator, mutex, logging formatter, loader lookup, or network operation runs in a steady-state probe.
- Fixed pools and index validation make overload a telemetry loss condition rather than memory corruption.
- Signal-context probes are unsupported initially; the runtime must not advertise async-signal safety.

ABI fixtures will check structure size/alignment for every supported target, version rejection, zero-token behaviour, recursion, invalid/out-of-order tokens, repeated initialization, shutdown, and cross-language linkage. Those tests are release requirements, not claims about this draft.
