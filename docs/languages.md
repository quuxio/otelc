# Language adapter implementation

Each additional language lands separately on `master` after its unchanged-source examples, configuration conformance, exceptional exits, SDK/Collector decoding, coverage, documentation and paired benchmark pass. All GitHub work uses quuxio.

| Order | Language | Function metrics implementation |
| --- | --- | --- |
| Existing | C / C++ | Clang wrapper and LLVM 22 pass; exception-enabled C++ |
| 1 | Python | CPython 3.12+ monitoring and Python SDK; implemented |
| 2 | JavaScript | Node loader transform; next |
| 3 | TypeScript | Language-aware Node transform; planned |
| 4 | Java | Java agent and method bytecode instrumentation; planned |
| 5 | Go | External build adapter with generated probes; planned |
| 6 | Rust | External Cargo/source adapter with generated probes; planned |

The common TOML policy remains the configuration entry point. Existing annotations are optional. The adapter must reject capabilities it cannot execute; a language implementation does not imply support for spans, every framework or every lifetime boundary. Automatic C++ lifetimes remain separate from the current explicit guard prototype.

See the [common configuration](common-configuration.md), [Python guide](python.md), [developer 101](developer-101.md) and [support matrix](support.md).
