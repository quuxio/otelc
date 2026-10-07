# Compiler integration

## Callback backend

The local callback backend uses Clang's documented [`-finstrument-functions`](https://clang.llvm.org/docs/ClangCommandLineReference.html#cmdoption-clang-finstrument-functions) option. The compiler emits callbacks whose arguments identify the function and caller:

```c
void __cyg_profile_func_enter(void *function, void *call_site);
void __cyg_profile_func_exit(void *function, void *call_site);
```

These addresses are process-local identities, not stable function IDs. There is no native exception-status argument. `call_site` is not sufficient to infer a business operation or guarantee stack recovery after unwinding.

The runtime and native shim are compiled without instrumentation. The wrapper must reject an application that already defines these callbacks or links an incompatible instrumentation runtime. It must avoid double-instrumenting objects and preserve compiler argument order and exit status.

## Selection

The wrapper instruments explicitly selected application translation units. Function filters are matched against the manifest's demangled names before execution and compiled into an immutable address-to-admission table. Exclusions win. Unknown functions are rejected by default. Selecting a function in an uninstrumented library does not make that function observable.

Clang does not inherit every GCC instrumentation exclusion flag. Do not translate a configuration into GCC-only switches and assume it works on Apple Clang. In the callback backend, an excluded function in an instrumented translation unit still receives callbacks, even if the runtime rejects them immediately. Overhead must be measured on that path.

The first backend instruments before inlining. Inlined logical function callbacks, address-taken functions, templates, and compiler-generated functions can change code size and optimization. The manifest and compiler fixture suite must explain the observed granularity. `-finstrument-functions-after-inlining` is a separate future capability because it measures surviving machine functions and can remove observations expected by users.

## Build wrapper

The implemented `quux-otelc ... clang` and `quux-otelc ... clang++` forms wrap the real driver. Compile-only invocations add probes only to selected source inputs; link-only invocations add the matching runtime once and build the final manifest. Preprocessing, dependency generation, assembly-only output, compiler queries, and configure-time checks pass through without instrumentation.

The wrapper parses response files using the selected driver's rules, handles paths containing spaces, and retains the application's optimization, debug, target, sysroot, and deployment flags. It records the effective compiler version, target triple, probe backend, and relevant flags in the manifest. Unsupported LTO, mixed runtimes, or cross-compilation combinations fail a capability check before producing a misleading instrumented artifact. With `backend=llvm`, the wrapper explicitly selects the compiler matched to the built plugin; Apple Clang remains a separate callback lane.

The direct driver wrapper comes first. A CMake adapter will then use compiler launchers for compilation and an explicit link integration. A launcher alone cannot ensure runtime linkage. `compile_commands.json` helps inspect selection but is not a replacement for capturing the final link. General-purpose `build` wrapping waits until a tested CMake workflow exists.

## Function metadata

For the callback prototype, read the final ELF or Mach-O binary and its optional matching debug companion. Use the image's ELF build ID or Mach-O UUID, the link-time function address, a retained mangled name, a display name, and available source metadata. Apply the image's load slide to resolve callbacks under ASLR. Validate the runtime architecture and manifest identity before enabling probes.

The initial deployment bundle contains the executable and an adjacent `.otelc.json` manifest, or an explicit manifest path selected by `quux-otelc run`. A stripped executable must retain the manifest generated from the unstripped final link and an identity that still matches the stripped image. Missing debug information means missing source metadata, not invented file names or line numbers.

Static application code in one executable is the first supported lane. Shared objects/dylibs, `dlopen`, `dlclose`, interposed symbols, and linker-identical-code folding need separate identity and lifetime tests before support. A function address alone cannot distinguish arbitrary merged symbols.

## Control-flow limits

Local checks on Apple Clang 21 and Homebrew Clang 21.1.8/23.1.2 showed a missing exit callback on a thrown C++ exception. The [research record](research.md) provides the fixture. This is a reason to avoid claiming balanced exception tracing with ordinary callbacks.

The callback timing lane requires selected C++ translation units to have exceptions disabled already. The wrapper rejects exception-enabled timing builds in that lane. C code must follow the normal-return contract; `setjmp`/`longjmp` and cancellation remain unsupported. A detected mismatched exit discards abandoned frames and records lost observations, but recovery is not proof that all such control flow can be detected.

The same function can recur with indistinguishable legacy callback addresses, so an apparent address match after an unsupported unwind is not sufficient evidence of valid nesting. Reliable nested traces are deferred to the token-based backend.

## LLVM backend

The local LLVM 22 pass uses LLVM's new pass manager and a matched upstream toolchain-specific plugin build. It implements exception-aware function timing; sampled traces and descriptor-driven registration remain proposals. It selects eligible functions after a documented optimization boundary, retains a function-address inventory, and injects `otelc_function_enter_v1`/`otelc_function_leave_v1` calls. The returned token is local to one invocation and survives recursive calls.

The local pass instruments normal returns and escaping exception cleanup exits, including `invoke`, `landingpad`, and `resume` paths. Only escaping exceptions mark an invocation as an exceptional exit; an exception caught inside the same function does not. If the pass cannot prove balanced cleanup for a function, it must exclude that function with a reason in the manifest. Unsupported exception personalities and Windows funclets are explicit capability failures.

Probe calls must remain observably ordered without unnecessarily prohibiting unrelated optimization. Descriptors and symbols must survive the chosen linker settings. Tail calls, `musttail`, `noreturn`, LTO, sanitizer combinations, and optimization-induced merging need their own fixtures; the pass cannot simply insert a call before every textual `ret` and declare completion.

Apple Clang does not promise the same plugin surface or LLVM ABI as an upstream Homebrew toolchain. Use a matched upstream Clang/LLVM toolchain for pass development and prove it independently on macOS and Linux. Ordinary Apple Clang callback builds remain a distinct compatibility lane.
