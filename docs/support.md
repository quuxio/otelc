# Support matrix and limits

## Current state

The local callback and LLVM 22 backends have passed native C/C++ and OTLP checks on macOS ARM64. The LLVM lane handles Itanium C++ exception unwinding; the opt-in lifetime guard supports bounded object metrics but requires source edits. Automatic source-free lifetime instrumentation remains TODO. There is no supported release yet. The tables retain rollout requirements; Linux and other platforms still require native qualification. See [current implementation and evidence](local-implementation.md).

All target languages must meet the [source-free contract](design.md#source-free-instrumentation-contract). The following routes describe current behaviour and planned adapters separately.

## Languages

| Language | Initial route | Planned support |
| --- | --- | --- |
| C | Clang callbacks | M1: synchronous normal-return functions |
| C++ | Clang callbacks, then LLVM pass | Local callbacks: selected `-fno-exceptions` code; local LLVM: exception-enabled timing; traces planned |
| Objective-C / Objective-C++ | Clang with language-specific fixtures | Later: method names, exceptions, blocks, ARC, and messaging boundaries |
| Swift | Matched compiler integration or Swift-specific pass | Later: begin with synchronous native functions; async requires a separate adapter |
| Rust | Rust 1.98.1 generated-source and Cargo compiler wrapper | Implemented synchronous named functions, methods, generics, threads, panic unwinding and live controls; async/closures, lifetimes/traces unavailable |
| Fortran / Zig | Validated compiler adapter | Exploratory; no current support commitment |
| Go | Go 1.26+ compiler overlays and Go SDK | Implemented unchanged files/module projects, functions/methods/callbacks, goroutines, defer/panic/recover and live controls; workspaces/cgo, lifetimes/traces unavailable |
| Java | JDK 21+ agent and ASM bytecode body probes | Implemented selected methods/constructors, exceptions, recursion, executor threads, optional annotations and live controls; lifetimes/traces unavailable |
| Python | CPython 3.12+ monitoring, Python OTLP SDK | Implemented function timing, exceptions, recursion, threads, generators, async/cancellation and live control; lifetimes/traces remain unavailable |
| JavaScript | Node 24.11+ in-memory loader transform, JavaScript SDK | Implemented ESM/CommonJS function timing, async/generators, constructors, exceptions and live controls; lifetimes/traces unavailable |
| TypeScript | TypeScript 6.0.3 compiler emit and Node in-memory probes | Implemented typed functions, enums/namespaces, decorators, exceptions, original source maps and live metrics; lifetimes/traces unavailable |
| .NET | Runtime/agent ecosystem | Exploratory; outside the current target-language list |

Using LLVM somewhere in a compiler pipeline does not imply that it accepts our plugin, shares the same LLVM ABI, or preserves the required language semantics. Every new language must supply its own build and control-flow fixtures.

## Platforms

| OS / architecture | Intended tier | Conditions |
| --- | --- | --- |
| macOS ARM64 | First implementation target | Mach-O identity, Apple callback lane; upstream LLVM for plugin lane |
| Linux x86-64 | M1 target | ELF identity; tested Clang/glibc combination |
| Linux ARM64 | M1 target | Native execution required; cross-compilation alone is insufficient |
| macOS x86-64 | Later | Independent clock, ABI, loader, and native fixture results |
| Windows x86-64 / ARM64 | Later | PE/COFF, DLL lifetime, native TLS, and exception funclets |
| Linux musl / FreeBSD / RISC-V | Exploratory | Separate runtime and toolchain qualification |

A release lists exact tested compiler/runtime versions. "Clang supports the target" is insufficient evidence that otelc works there. Apple Clang and upstream Clang have separate capabilities even when their displayed major numbers look similar.

## Initial callback boundaries

| Scenario | M1 contract |
| --- | --- |
| Normal synchronous returns and bounded recursion | Required |
| Concurrent ordinary native threads | Required within configured thread/queue limits |
| Already-disabled C++ exceptions | Required for selected C++ translation units |
| C++ throw/unwind, C `longjmp`, cancellation | Unsupported |
| Static application code in one executable | Required |
| Stripped executable plus matching retained manifest | Required |
| Missing source debug information | Function timing remains usable; file/line absent |
| Shared libraries / dynamic load and unload | Deferred |
| Sanitizers, LTO, linker symbol folding, tail-call variants | Deferred until explicit fixtures qualify the combination |
| Async, coroutines, fibers, task migration | Unsupported |
| Distributed trace context | Requires a later adapter |
| Fatal signal / `abort` / `_exit` | No guaranteed flush |
| Fork while instrumented | Unsupported until child-disable behaviour is validated |
| Signal-handler instrumentation | Unsupported; no async-signal-safety claim |

The wrapper rejects known unsupported combinations before building. Cases that cannot be identified reliably from compiler flags remain documented execution requirements. Recovery counters are diagnostic evidence, not a substitute for these limits.
