# Roadmap and release validation

## Milestones

Milestones are ordered technical contracts. No release date or performance claim is implied by this design. Each milestone must land with useful working behaviour, documentation, and evidence for its stated platforms.

### M0: design and repository foundation

- Publish the quux repository, README, design documents, configuration example, and contribution guidance.
- Match the quality/traffic badge conventions used by `fixdecoder_rs`.
- Create the SonarQube Cloud project and enforce a project-level Previous version new-code policy.
- Scan exact clean Git commits and identify repository-tooling coverage separately from future product coverage.

### M1: callback timing vertical slice

Deliver `doctor`, direct `clang`/`clang++` wrapping, `inspect`, and `run`, plus a Rust runtime and OTLP/HTTP metric exporter. Start on macOS ARM64; qualify Linux x86-64 and ARM64 through native fixtures before calling the milestone cross-platform.

Acceptance requires a minimal C application and an existing `-fno-exceptions` C++ application to build without source instrumentation edits, run normally, and produce verifiable call counts and duration histograms in a local Collector. Selected/unselected functions, recursion, thread admission, bounded overload, ASLR, manifest mismatch, stripping, shutdown, and Collector outage must have observable expected results. No trace or exception-support claim is part of this milestone.

### M2: selective LLVM probes and reliable traces

Build a new-pass-manager plugin for an explicitly matched upstream LLVM toolchain. Emit versioned descriptors and invocation tokens, omit excluded probes, and support normal returns plus escaping C++ exceptions for a tested exception personality. Retain callback support as a separately reported backend.

Acceptance requires nested and recursive sampled traces with correct parents, valid IDs/timestamps, inherited sampling, and coherent discard under loss. Exception fixtures must prove that every supported exit closes its invocation, catches inside a function remain normal, and unsupported control flow is excluded visibly. IR verification and optimized native execution are both required. Cross-thread parenting is not implied.

### M3: build and operational integration

Add a tested CMake compile/link adapter, runtime status and filter changes over an owner-only local socket, packaging of binary/runtime/manifest, and a documented manual or framework context handoff. Define the support contract for modules and fork before enabling them.

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

Use deterministic unit tests at ownership and parsing boundaries, native integration fixtures for compiler behaviour, fuzzing for manifests/configuration, and concurrency tools for the unsafe runtime. Maintain at least 90% line coverage in product code and material new changes, with documented evidence-backed exceptions. High coverage alone does not prove correct FFI or lock-free publication.

## Performance methodology

Compare the same application fixture, compiler, flags, CPU policy, and workload in four lanes: uninstrumented baseline, probes linked but runtime disabled, rejected-function callbacks, and admitted timing with export. Add the LLVM filtered lane and sampled tracing lane when implemented.

Record toolchain/OS/architecture, CPU model, binary size, allocation counts, RSS, throughput, wall-clock and CPU time, and per-invocation p50/p95/p99 overhead. Separate initialization/TLS registration from steady state. Run enough repetitions to report variance, and retain raw machine-readable results alongside the human comparison.

Proposed acceptance targets are zero steady-state producer heap allocations and locks, memory staying inside the configured budget, bounded shutdown, and at most 5% throughput regression on a representative application with selective timing. That workload target is provisional; it is not a measured result or a claim that every tiny function can be timed cheaply. Publish absolute nanoseconds per probe as well as workload percentages.

## Release gate

A supported release requires successful tests and static analysis, a passing SonarQube gate on its exact commit, declared tested platform versions, benchmark evidence, versioned ABI/configuration compatibility, no unresolved known telemetry corruption, and current usage/limitations documentation. Signed commits and Conventional Commits follow the repository conventions.

The first runtime release also needs an explicit review of the repository's chosen license and distribution model. This design does not grant a separate runtime linking exception.
