# Design decisions

## 001: use existing callbacks to prove timing

**Decision:** begin with Clang's function callbacks and a Rust runtime. This tests the complete build/probe/aggregation/OTLP path without making an LLVM plugin the first prerequisite.

**Tradeoff:** callback filtering cannot remove injected-call overhead, and the legacy ABI does not reliably close exceptional exits. The backend is explicitly normal-return timing only. A custom LLVM pass is required by the next milestone.

**Alternatives:** source rewriting has macro/template/formatting costs; an LLVM-first build adds toolchain complexity before the runtime is proven; Linux-only external tracing does not meet the macOS/native-function goal.

## 002: publish completed observations

**Decision:** keep a bounded producer stack and enqueue one complete timing observation at a valid exit.

**Tradeoff:** entry/exit stack work remains on the application thread, but queue loss cannot strand a half-pair in the consumer. Lost complete observations still bias call counts and histograms; expose loss counters and describe observed-completion semantics.

**Alternative:** two independent queue events are cheaper to describe but require recovery when either half is missing. They are not the initial metric contract.

## 003: keep telemetry processing outside probes

**Decision:** use fixed native keys and monotonic timestamps in probes; aggregate and export from worker paths.

**Tradeoff:** fixed pools reserve memory and need rigorous ownership/publication tests. This avoids per-call SDK objects, serialization, and networking. Use established Rust OTLP libraries on the worker side rather than implement a second telemetry protocol.

## 004: make metrics independent of trace sampling

**Decision:** aggregate every admitted completion received by the worker. Sample whole trace roots and inherit that decision for descendants only when a supported trace backend exists.

**Tradeoff:** metrics still generate probe traffic when a trace is unsampled. They remain interpretable without applying sampling multipliers. Queue/admission losses remain visible rather than being hidden by estimated totals.

## 005: qualify compilers and languages separately

**Decision:** Apple Clang callback support and upstream LLVM plugin support are separate lanes. A compiler's use of LLVM is not sufficient to admit a new language.

**Tradeoff:** there will be a smaller supported matrix initially. It gives each language's exceptions, panic, coroutines, symbols, and ABI a concrete validation boundary.

## 006: version the smallest useful ABI

**Decision:** the LLVM probe ABI has an immutable function descriptor, an opaque per-invocation token, and a leave outcome. Version symbols and descriptors explicitly.

**Tradeoff:** the legacy callback adapter cannot manufacture all token-backend guarantees. Context propagation and manual APIs are added only when an implemented adapter needs them.

## 007: use one versioned TOML configuration

**Decision:** share one schema for build selection, bounded runtime policy, and export settings. Use an explicit schema version, reject unknown keys, and make exclusions win.

**Tradeoff:** additions must preserve schema compatibility. TOML fits the Rust tooling and the mainly scalar/list configuration without requiring YAML semantics or configuration scripting.

## 008: use quux ownership and a distinct executable name

**Decision:** GitHub home is `quuxio/otelc`; product identity is otelc by quux; intended executable is `quux-otelc`. OpenTelemetry already uses `otelc` for its Go compiler instrumentation project.

**Tradeoff:** the executable is longer than the repository name, but users can install both tools without a command collision. This project is independent of OpenTelemetry. Public package names are provisional until publishing checks are completed.

## 009: match the existing repository license and quality conventions

**Decision:** start with AGPL-3.0 and the SonarQube/traffic badge set from `fixdecoder_rs`. Require project-level Previous version new-code policy and use the full commit SHA as each scan's project version.

**Tradeoff:** the license contains no special runtime exception. Review the intended runtime distribution model before its first release. Quality badges currently describe repository validation tooling; they must not be presented as runtime correctness or performance evidence.
