# otelc documentation

otelc is quux's native instrumentation project. These documents specify intended behaviour; they do not imply that the compiler wrapper or runtime is available. The [roadmap](roadmap.md) defines the evidence required to turn each proposal into a supported feature.

Read the [system design](design.md) first, then the documents for the boundary you are working on.

| Area | Document |
| --- | --- |
| Product scope and component boundaries | [System design](design.md) |
| Compiler callbacks, symbols, and LLVM instrumentation | [Compiler integration](instrumentation.md) |
| Instrumented-code/runtime interface | [Probe ABI](abi.md) |
| Hot path, buffers, overload, and lifecycle | [Runtime](runtime.md) |
| Configuration and user-facing commands | [Configuration](configuration.md) |
| Metrics, traces, clocks, and export | [Telemetry](telemetry.md) |
| Language, OS, and execution-model support | [Support matrix](support.md) |
| Delivery sequence and release acceptance | [Roadmap and validation](roadmap.md) |
| Architectural choices | [Design decisions](decisions.md) |
| Prior art and compiler feasibility evidence | [Related work](research.md) |
| CI, SonarQube, badges, and local validation | [Repository quality](quality.md) |

The [example TOML](../examples/otelc.toml) illustrates the proposed schema. Nothing in that file enables telemetry in this design-only checkout.
