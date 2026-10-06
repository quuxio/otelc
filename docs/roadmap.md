# Roadmap and release validation

## Product requirement

Every target language must satisfy the [source-free instrumentation contract](design.md#source-free-instrumentation-contract). External configuration and build/launch changes are allowed; application source edits are not an installation step or a fallback for an unsupported adapter. Function and supported object/resource lifetime selection must cover application code. A language-aware pre-parser may inject code or annotations into generated copies; existing annotations may guide selection, but adding them to original source cannot be required.

The [common schema-2 configuration](common-configuration.md), validator and JSON resolver are implemented for all target IDs. Native C/C++, Python, JavaScript, TypeScript, Java and Go use the shared policy now; the remaining adapters must consume it and pass its conformance rules.

## Milestones

Milestones are ordered technical contracts. No release date or performance claim is implied by this design. Each milestone must land with useful working behaviour, documentation, and evidence for its stated platforms.

### M0: design and repository foundation

- Publish the quux repository, README, design documents, configuration example, and contribution guidance.
- Match the quality/traffic badge conventions used by `fixdecoder_rs`.
- Create the SonarQube Cloud project and enforce a project-level Previous version new-code policy.
- Scan exact clean Git commits and identify repository-tooling coverage separately from future product coverage.

### M1: callback timing vertical slice

The local macOS ARM64 slice is implemented and tested; [implementation evidence](local-implementation.md) records its scope. This milestone remains open until native Linux qualification and the remaining release gates are satisfied.

Deliver `doctor`, direct `clang`/`clang++` wrapping, `inspect`, and `run`, plus a Rust runtime and OTLP/HTTP metric exporter. Start on macOS ARM64; qualify Linux x86-64 and ARM64 through native fixtures before calling the milestone cross-platform.

Acceptance requires a minimal C application and an existing `-fno-exceptions` C++ application to build without source instrumentation edits, run normally, and produce verifiable call counts and duration histograms in a local Collector. Selected/unselected functions, recursion, thread admission, bounded overload, ASLR, manifest mismatch, stripping, shutdown, and Collector outage must have observable expected results. No trace or exception-support claim is part of this milestone.

### M2: selective LLVM probes and reliable traces

The local LLVM 22 timing pass is implemented. The explicit object-lifetime guard is an interim opt-in prototype and does not satisfy source-free lifetime support. M2 remains open for automatic class lifetimes, sampled traces, descriptors and full release qualification. Build a new-pass-manager plugin for an explicitly matched upstream LLVM toolchain. Emit versioned descriptors and invocation tokens, omit excluded probes, and support normal returns plus escaping C++ exceptions for a tested exception personality. Retain callback support as a separately reported backend.

Acceptance requires nested and recursive sampled traces with correct parents, valid IDs/timestamps, inherited sampling, and coherent discard under loss. Exception fixtures must prove that every supported exit closes its invocation, catches inside a function remain normal, and unsupported control flow is excluded visibly. IR verification and optimized native execution are both required. Add automatic class lifetime probes through a Clang frontend/code-generation adapter, retaining type and construction/destruction metadata before optimisation; the general LLVM function pass alone is not proof of complete class coverage. Validate successful construction, failed/delegating constructors, destructor cleanup, copy/move as distinct object instances, implicit/trivial operations, inheritance, placement-new/address reuse and cross-thread destruction without editing fixtures or changing class layout. Unsupported cases must be reported. Cross-thread parenting is not implied.

### M3: source processing, build and operational integration

Live LLVM metrics status/on/off control and existing Clang function annotations are implemented; live function-filter changes and general source injection remain open. Add a language-aware source-processing stage and a tested CMake compile/link adapter, runtime filter changes over an owner-only local socket, packaging of binary/runtime/manifest, and adapter-driven or framework context handoff without application source edits. Define the support contract for modules and fork before enabling them.

Source processing must preserve the original files and emit generated input in a separate build directory or memory. Parse with the actual language/compiler context; support externally configured selection without annotations, optional existing annotations, injected annotations/probes, exclusion priority and double-instrumentation detection. Validate macro/template/preprocessor behaviour, include/module resolution, diagnostics/source maps, reproducible output and cache invalidation when source, configuration or tool versions change. Function results, exceptions and class layout/copy/move behaviour must match the plain build.

Acceptance includes policy changes while frames are open, preserved entry generations, process-owner access checks, unreachable control service behaviour, and a clean install/build/run path. Rejected entries must not become unmatched exits after a policy update. Dynamic enablement applies only to probes already present in the binary.

### M4: additional native languages and platforms

Investigate Objective-C, Swift synchronous functions, Rust synchronous functions, and Windows. Admit each only with a native compiler adapter, language-specific names/control-flow fixtures, ABI/lifetime evidence, and repeatable performance results. Async tasks and profiles remain separate contracts.

## Test strategy

| Boundary | Required evidence |
| --- | --- |
| Configuration | Valid/invalid schema, bounded arithmetic, precedence, empty includes, exclusion priority, secrets redaction |
| Manifest | ELF/Mach-O parsing, image identity mismatch, ASLR, stripped/debug companions, invalid metadata, bounded symbol cardinality |
| Runtime queue | Full/empty/wrap, producer/consumer publication, thread retirement/reuse, model checking, sanitizers |
| Probe ABI | Target layouts, version rejection, zero/invalid tokens, recursion, entry/exit ownership, no foreign unwind |
| Compiler wrapper | Compile/link classification, response files, spaces, compiler failure, preprocessor pass-through, existing hooks, unsupported flags |
| LLVM pass | IR verifier, multiple returns, exceptions/catches/rethrow, recursive unwind, tail calls, `noreturn`, optimized execution |
| Telemetry | Histogram/count correctness, cumulative restart intervals, trace parenting, whole-tree sampling/discard, clock adjustment |
| Export | Real local Collector decoding, partial success, bounded retries, duplicate ambiguity, offline backend, shutdown timeout |
| Platform | Native macOS ARM64 and Linux x86-64/ARM64 execution with exact fingerprints |

Use deterministic unit tests at ownership and parsing boundaries, native integration fixtures for compiler behaviour, fuzzing for manifests/configuration, and concurrency tools for the unsafe runtime. The local gate enforces at least 80% product line coverage as requested. A supported release targets at least 90% line coverage in product code and material new changes, with documented evidence-backed exceptions. High coverage alone does not prove correct FFI or lock-free publication.

## Performance methodology

Compare the same application fixture, compiler, flags, CPU policy, and workload in four lanes: uninstrumented baseline, probes linked but runtime disabled, rejected-function callbacks, and admitted timing with export. Add the LLVM filtered lane and sampled tracing lane when implemented.

Record toolchain/OS/architecture, CPU model, binary size, allocation counts, RSS, throughput, wall-clock and CPU time, and per-invocation p50/p95/p99 overhead. Separate initialization/TLS registration from steady state. Run enough repetitions to report variance, and retain raw machine-readable results alongside the human comparison.

Proposed acceptance targets are zero steady-state producer heap allocations and locks, memory staying inside the configured budget, bounded shutdown, and at most 5% throughput regression on a representative application with selective timing. That workload target is provisional; it is not a measured result or a claim that every tiny function can be timed cheaply. Publish absolute nanoseconds per probe as well as workload percentages.

## Release gate

A supported release requires successful tests and static analysis, a passing SonarQube gate on its exact commit, declared tested platform versions, benchmark evidence, versioned ABI/configuration compatibility, no unresolved known telemetry corruption, and current usage/limitations documentation. Signed commits and Conventional Commits follow the repository conventions.

The first runtime release also needs an explicit review of the repository's chosen license and distribution model. This design does not grant a separate runtime linking exception.

## TODO: language adapters

Python, JavaScript, TypeScript, Java and Go function timing are implemented; the remaining integrations are being completed sequentially. All must instrument selected application code without source edits; users cannot be required to add decorators, annotations or macro attributes. Injected annotations and optional existing annotations are allowed. Keep internal test apps as the reproducible correctness and benchmark suite; external repositories are optional later qualification.

- [x] JavaScript function timing: Node in-memory transforms, JavaScript SDK export, optional comments, live controls, source maps, unchanged ESM/CommonJS apps and benchmarks are implemented. Automatic lifetimes and spans remain future capabilities.
- [x] TypeScript function timing: compiler emission, preserved original identities/source maps, unchanged typed/annotated apps, decorators, live controls and a compiler-only paired benchmark are implemented. Automatic lifetimes and spans remain unavailable. Original investigation: investigate a build adapter or Node module-loader transform, using generated output or in-memory modules while keeping original `.ts`/`.js` files unchanged. Inject selection externally; require no user-added imports, wrappers or decorators in original application code. Use the JavaScript SDK for export/context. Test source maps, exceptions, promise rejection, async/await, generators, ESM/CommonJS and Node/browser differences. Library auto-instrumentation alone does not cover arbitrary application methods.
- [x] Java function timing: JDK 21+ load-time bytecode agent, configured methods/constructors, exceptions, recursion, executor threads, optional compiled annotations, SDK export, live controls and source-preserving benchmarks are implemented. Constructor admission follows the base constructor. AspectJ, automatic lifetimes and spans remain unavailable.
- [x] Python function timing: CPython monitoring, SDK export, optional comment metadata, live controls, unchanged-source apps and benchmarks are implemented. Automatic lifetimes and spans remain future capabilities. Original investigation: an externally installed launch/import hook, in-memory AST transform or interpreter profiling adapter for configured application functions; require no user-added source decorators, imports or manual spans. Use the Python SDK and test normal return, exceptions, recursion, coroutine cancellation, generators and import-order effects. Benchmark profiling and wrapping separately; standard framework instrumentation alone is insufficient.
- [ ] Rust: investigate a Cargo/rustc wrapper with compiler/MIR integration for selected functions and supported value initialisation/move/drop boundaries. Required user-added procedural-macro attributes do not meet the source-free contract; investigate whether a generated-source stage can inject metadata/probes safely, with compiler/MIR integration for boundaries it cannot preserve. Test normal return, recursion, monomorphisation, panic unwinding, panic=abort and Drop without allowing a panic across the probe ABI. Treat async futures, poll/resume, cross-thread movement and cancellation as a separate adapter contract. Retain Rust symbol names and pin a compatible compiler; LLVM function probes alone do not prove typed lifetime coverage.
- [x] Go function timing: external compiler overlays, private alternate module manifests, Go SDK export, optional comments, preserved direct recovery/named returns, goroutines, live controls and unchanged-source benchmarks are implemented. Legacy nil-panic mode, workspaces, cgo, automatic lifetimes and spans remain unavailable. An adapter to upstream Go compile instrumentation remains a future alternative; this implementation does not assume its library rules cover arbitrary application functions.
- [ ] Source-processing adapter: parse unchanged original input with language-aware tooling; read optional existing annotations/metadata; inject code or annotations into generated copies; validate unannotated external selection, exclusion precedence, original-file hashes, source maps, semantic preservation and cache identity.

For every adapter, require common schema/resolved-policy conformance, unchanged-source hash checks, identical plain/instrumented application results, normal and exceptional exit evidence, externally configured selection, documented lifetime boundaries and paired benchmarks with loss reporting. Prefer compiler/agent injection over mechanisms that require manual adoption inside application code.

Use language-appropriate SDKs and context handling rather than routing managed-language probes through native thread slots. Share selection intent, service identity, metric definitions, OTLP destinations and plain-versus-instrumented benchmark reporting where semantics allow.

Primary references: [Node module customization hooks](https://nodejs.org/api/module.html#customization-hooks), [AspectJ weaving](https://eclipse.dev/aspectj/doc/latest/devguide/ltw.html), [Java agent](https://opentelemetry.io/docs/zero-code/java/agent/), [Python instrumentation](https://opentelemetry.io/docs/zero-code/python/), and [Go compile integration](https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation/blob/main/docs/getting-started.md).
