# otelc engineering instructions

- Read `~/.agents/AGENTS.md` when present.
- Use the verified `quuxio` GitHub identity exclusively.
- The pre-rebuild implementation is preserved on `archive/pre-rebuild-2026-10-21` at `c4af0bd18b668b0dbdb42bf0b08353b23b9bfd7c`. Reuse proven components selectively.
- One common TOML policy drives official tooling and our remaining probes. Never require developer-authored imports, annotations, decorators, guards or instrumentation bootstrap.
- Metric collection and emission must each switch live in the same application instance. Startup-off must support later enable. A restart/rebuild is not an acceptable metrics-control fallback.
- Keep tracing independent of metric switches. Distinguish object destruction/drop, observed GC and resource release.
- Implement in order: Java, TypeScript/JavaScript, Python, Go, LLVM backend, C, C++, Rust. Do not claim future adapters are implemented.
- Match example workloads/checksums and benchmark plain plus the four live states. Retain raw results and toolchain/build identity.
- Run `make check`, require at least 80% component coverage, review the final change, and preserve required GitHub/Sonar checks before merge.
