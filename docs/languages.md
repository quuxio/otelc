# Language adapter implementation

Each additional language lands separately on `main` after its unchanged-source examples, configuration conformance, exceptional exits, SDK/Collector decoding, coverage, documentation and paired benchmark pass. All GitHub work uses quuxio.

| Order | Language | Function metrics implementation |
| --- | --- | --- |
| Existing | C / C++ | Clang wrapper and LLVM 22 pass; exception-enabled C++ |
| 1 | Python | CPython 3.12+ monitoring and Python SDK; implemented |
| 2 | JavaScript | Node in-memory transform and JavaScript SDK; implemented |
| 3 | TypeScript | TypeScript compiler and in-memory Node probes; implemented |
| 4 | Java | Java agent and method bytecode instrumentation; implemented |
| 5 | Go | Go compiler overlays and Go SDK; implemented |
| 6 | Rust | Rust 1.98.1 generated body guards, qualified async completion/cancellation timing, Cargo compiler wrapper and Rust SDK; implemented |

The common TOML policy remains the configuration entry point. Existing annotations are optional. The adapter must reject capabilities it cannot execute; a language implementation does not imply support for spans, every framework or every lifetime boundary. Automatic C++ lifetimes remain separate from the current explicit guard prototype.

See the [common configuration](common-configuration.md), [Python guide](python.md), [JavaScript guide](javascript.md), [TypeScript guide](typescript.md), [Java guide](java.md), [Go guide](go.md), [Rust guide](rust.md), [developer 101](developer-101.md) and [support matrix](support.md).
