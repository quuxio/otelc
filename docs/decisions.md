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

**Decision:** share one schema for source/function/lifetime selection, bounded runtime policy and export settings across all language adapters. Schema 2 and its JSON resolver are implemented; schema 1 remains the legacy native format. Language-specific backend settings belong under `adapters.<language>`. Use an explicit schema version, reject unknown keys, and make exclusions win.

**Tradeoff:** additions must preserve schema compatibility. TOML fits the Rust tooling and the mainly scalar/list configuration without requiring YAML semantics or configuration scripting.

## 008: use quux ownership and a distinct executable name

**Decision:** GitHub home is `quuxio/otelc`; product identity is otelc by quux; intended executable is `quux-otelc`. OpenTelemetry already uses `otelc` for its Go compiler instrumentation project.

**Tradeoff:** the executable is longer than the repository name, but users can install both tools without a command collision. This project is independent of OpenTelemetry. Public package names are provisional until publishing checks are completed.

## 009: match the existing repository license and quality conventions

**Decision:** start with AGPL-3.0 and the SonarQube/traffic badge set from `fixdecoder_rs`. Require project-level Previous version new-code policy and use the full commit SHA as each scan's project version.

**Tradeoff:** the license contains no special runtime exception. Review the intended runtime distribution model before its first release. Quality badges currently describe repository validation tooling; they must not be presented as runtime correctness or performance evidence.

## 010: require instrumentation without application source edits

**Decision:** all target languages use external selection/configuration and compiler, loader or agent adapters. Users do not need to add application imports, annotations, decorators, macro attributes, guard members or manual probe calls. A language-aware pre-parser may inject code and annotations into generated copies or in-memory compiler input and may read existing source annotations as optional metadata. External configuration works for unannotated source. In-memory or out-of-tree generated transforms may inject telemetry while preserving original source files. Acceptance includes before/after source hashes and selected application-function coverage.

**Tradeoff:** each language needs its own validated adapter and lifetime semantics. Standard library auto-instrumentation does not prove application-function coverage. The existing C++ guard remains an opt-in prototype; automatic class lifetimes are an open requirement. See the [source-free contract](design.md#source-free-instrumentation-contract).

## 011: separate policy validation from adapter execution

**Decision:** validate one schema-2 document and resolve it for an enabled language. Common selection, annotation/lifetime intent, telemetry, limits and resource/export settings survive resolution. Language-specific backend/buffer settings are separate. Provide resolved JSON for tooling outside Rust and an explicit support check.

**Tradeoff:** valid future settings may lack an executable adapter. Report those capabilities as unavailable and reject execution rather than ignore the request or replace automatic lifetimes with manual guards. See [common configuration](common-configuration.md).

## 012: consume semantic function annotations and toggle metrics admission

**Decision:** read optional `otelc.instrument` and `otelc.exclude` function annotations from Clang LLVM metadata, preserve unrelated annotations, and retain annotation selection in object markers/manifests. Opt-outs and external exclusions win. Provide an opt-in owner-only native LLVM metrics control socket; changes affect future entries, while already admitted tokens finish normally.

**Tradeoff:** general annotation injection, other language adapters, callback live controls and live function-filter updates remain separate work. Off-state probes/runtime threads remain present, so a plain baseline is still required when measuring total instrumentation overhead.
