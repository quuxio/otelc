# Support matrix and limits

## Current state

No product platform is supported yet: the runtime and compiler wrapper are not implemented. The tables describe intended rollout, not successful end-to-end tests. The only compiler evidence currently recorded is the focused macOS ARM64 callback check in [research.md](research.md).

## Languages

| Language | Initial route | Planned support |
| --- | --- | --- |
| C | Clang callbacks | M1: synchronous normal-return functions |
| C++ | Clang callbacks, then LLVM pass | M1: selected `-fno-exceptions` code; M2: validated exception-enabled code and traces |
| Objective-C / Objective-C++ | Clang with language-specific fixtures | Later: method names, exceptions, blocks, ARC, and messaging boundaries |
| Swift | Matched compiler integration or Swift-specific pass | Later: begin with synchronous native functions; async requires a separate adapter |
| Rust | Rust compiler integration and matched LLVM | Later: monomorphization, panic, inlining, and async need Rust-specific evidence |
| Fortran / Zig | Validated compiler adapter | Exploratory; no current support commitment |
| Go | Existing Go compile-time ecosystem | Outside initial scope |
| Java / .NET / Python / JavaScript | Existing runtime/agent ecosystems | Outside native compiler scope |

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
