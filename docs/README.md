# otelc documentation

otelc is quux's native instrumentation project. The [local implementation](local-implementation.md) documents the working native prototype, including exception-aware C++, object lifetime metrics and the local Docker viewer. These design documents also retain explicit future contracts. The [roadmap](roadmap.md) defines the evidence required to turn each proposal into a supported feature.

Read the [system design](design.md) first, then the documents for the boundary you are working on.

| Area | Document |
| --- | --- |
| Product scope and component boundaries | [System design](design.md) |
| Compiler callbacks, symbols, and LLVM instrumentation | [Compiler integration](instrumentation.md) |
| Instrumented-code/runtime interface | [Probe ABI](abi.md) |
| Hot path, buffers, overload, and lifecycle | [Runtime](runtime.md) |
| Shared policy for all language adapters | [Common configuration](common-configuration.md) |
| Configuration and user-facing commands | [Configuration](configuration.md) |
| Metrics, traces, clocks, and export | [Telemetry](telemetry.md) |
| Language, OS, and execution-model support | [Support matrix](support.md) |
| Docker Collector, Prometheus and Grafana | [Local metrics viewer](observability-stack.md) |
| Plain versus instrumented measurements | [Paired benchmarks](benchmarks.md) |
| Explicit C++ object lifetime guard | [Object lifetimes](object-lifetimes.md) |
| Delivery sequence and release acceptance | [Roadmap and validation](roadmap.md) |
| Architectural choices | [Design decisions](decisions.md) |
| Prior art and compiler feasibility evidence | [Related work](research.md) |
| CI, SonarQube, badges, and local validation | [Repository quality](quality.md) |

The [common TOML](../examples/common.toml) provides schema 2 for every target language; the [legacy example](../examples/otelc.toml) provides schema 1. The [local fixture configuration](../examples/local.toml) can be used with the implemented callback backend.

Local workflows: [metrics viewer](observability-stack.md), [paired benchmarks](benchmarks.md), [object lifetimes](object-lifetimes.md), and [language adapter TODOs](roadmap.md#todo-language-adapters).

For complete tested source and commands, start with the [developer 101 guide](developer-101.md).

See [language adapters](languages.md) for sequential implementation status and [Python instrumentation](python.md) for the first additional language.
