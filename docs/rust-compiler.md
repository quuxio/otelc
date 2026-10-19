# Rust compiler-hook backend work

This backend is in development. The current CLI still uses generated source guards; its documented source-column limitation remains. This work does not yet provide a selectable compiler backend or close issue #66.

## Expected behaviour

Instrument the compiler's function bodies after Rust has parsed the original source and expanded macros. Configuration and existing annotations must select original declarations through the shared policy. Preserve source files, function signatures, object layout, return values, panic payloads and source observations such as `column!`, `file!`, `line!`, `Location::caller()` and panic locations.

The initial compiler profile targets synchronous functions and closures. Async constructors must not be reported as completed future bodies. The existing source backend remains available for its separately qualified async support until compiler-level future completion is implemented and tested.

## Current evidence

A local prototype loads the LLVM pass with rustc `1.101.0-nightly`, commit `d080e7dff1b0fc54541545252818f8cccf995d05` (LLVM 23.1.1), using a pass built with LLVM 23.1.3 on macOS ARM64. This is a specific experimental pairing, not support for arbitrary nightly releases.

Both baseline and instrumented compilations use `--edition=2024 -O -g -Zmir-enable-passes=-Inline,-ForceInline -Ccodegen-units=1`. MIR inlining otherwise removes some selected bodies before the LLVM pass can observe them. The baseline must retain these flags too; overhead comparisons against a differently optimised binary would be misleading.

The prototype leaves original source untouched and preserves the tested column and caller-location output byte for byte. The real Rust SDK reports three completed calls, one completed tree and zero loss. Independently decoded Tempo data contains the selected parent and two children. This proves that small example only; it does not establish the broader source-preservation or language support contract.

The private v1 hooks keep each synchronous guard in aligned storage on that function's stack. They reuse SDK admission, context and completion handling. No application arguments are passed to the hooks. The original-source inventory reuses the source parser's selection and annotation rules, preserving declaration and body coordinates for compiler matching. Async entries carry an explicit unsupported reason.

## Acceptance gates before availability

- Integrate an explicit common-policy backend, doctor checks and reproducible SDK/pass builds. Reject unqualified compiler/pass combinations and existing compiler wrappers before compiling the application.
- Exclude SDK implementation source roots using matching build metadata, including generic SDK bodies emitted into an application module. A broad application selection must never instrument the telemetry runtime itself.
- Require unambiguous original declaration matching and successful entry-point mapping. Missing probes cannot pass merely because application output matches. Preserve resolved source/probe provenance with the executable.
- Retain split build/run operation, compiler identity, baseline flags and executable fingerprints. Running a built artefact must not require the compiler, original sources or SDK installation.
- Test direct, qualified, aliased and forwarded location macros; nested and same-line declarations; closures; Unicode; CRLF; attributes; tracked callers; and panic locations. Compare raw application channels, exit status and unchanged source hashes with a matched baseline.
- Decode actual OTLP spans for normal completion, recursion, threads and escaping/caught panics. Test cleanup, parent restoration, rejected admission and later recovery. Inconclusive or incomplete observations must remain visible.
- Enforce at least 80% coverage for the new Rust ABI, inventory/driver and LLVM pass, with the qualified compiler profile exercised in CI. Review the final published head and require all repository checks before merge.

Automatic async completion, task/service propagation, object lifetimes, Cargo integration and diagnostic invocation scopes require their own qualification; this prototype supplies none of those capabilities.
