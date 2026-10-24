# LLVM compiler backend

<!-- markdownlint-disable MD013 -->

Status: compiler component candidate, profile `llvm22-itanium-v1`. It inserts function entry and exit calls into original C/C++ compilation units or an ordinary LLVM IR module. It reuses the archived pass, with fixes for common-policy Unicode wildcards, C++ operator names and application collisions with the reserved probe ABI. This component does not yet supply the native SDK, public application launcher, live metric controls, context propagation or automatic object/resource lifetimes. C and C++ therefore remain incomplete adapters.

## Build and check

Install LLVM 22 with its development headers and profiling tools. On Apple Silicon:

```sh
brew install llvm@22
export OTELC_LLVM_BIN=/opt/homebrew/opt/llvm@22/bin
make llvm-plugin
make llvm-check
```

Linux CI uses Ubuntu 24.04 and `bash ci/install_llvm22.sh`, selecting `/usr/lib/llvm-22/bin`. The script installs from the [official LLVM APT repository](https://apt.llvm.org/), checks its published signing-key fingerprint and restricts that key to the LLVM repository. It requires `sudo`; ordinary application builds do not. `LLVM_CONFIG=/path/to/llvm-config` can select another installed LLVM 22 toolchain. Discovery requires Clang, Clang++ and llvm-config to report the same stable version; it does not install tools or silently substitute another major version.

`make llvm-plugin` writes a new `build/llvm-plugin` directory and prints a verified JSON record. Choose a new directory for another build:

```sh
make llvm-plugin LLVM_OUTPUT=build/llvm-plugin-second
```

The record binds the compiler/plugin source, compiler identities, actual compile command, compilation database, diagnostics and resulting library. The Python API retains an immutable caller-held build result; an edited on-disk record does not replace it. The [native analysis pipeline](native-analysis.md) uses that actual build context for C++ scanning. This is a local component binding, not a signature or whole-program provenance claim. `source_complete=false` stays explicit.

## Common selection and probe contract

`otelc.llvm_backend.environment(policy, 'c')` or `'cpp'` translates the common `[functions] include/exclude` lists into the compiler environment. `*` matches zero or more characters, `?` matches one Unicode character, and C++ punctuation such as `[]` is literal. Exclusion wins. C++ selection uses demangled function signatures. Metric switches do not change which calls the compiler inserts; the future runtime must apply those switches live.

The component API builds and verifies the library, then returns selection separately:

```python
from pathlib import Path
from otelc.config import Policy
from otelc.llvm_backend import build, environment, verify

policy = Policy.load(Path('examples/otelc.toml'))
plugin = build(Path('build/my-llvm-plugin'))
record = verify(plugin)
compiler_environment = environment(policy, 'cpp')
```

This is tooling code, not code developers add to an application. A frontend can pass the returned library to Clang's `-fpass-plugin=...` option with that environment. Source-file selection, build-system interception, SDK linking and public native launch commands remain frontend work. Passing every source file directly to this component does not enforce `[sources]` selection.

The inserted C ABI is:

```c
uint64_t otelc_function_enter_v1(const void *function);
void otelc_function_leave_v1(uint64_t token, int32_t unwinding);
```

Probes must return normally, must not throw, and must preserve application behaviour. `unwinding` is zero for a normal return and one when an Itanium exception leaves the selected function. The entry token is passed to the matching exit. Existing application definitions, globals, incompatible types or non-C calling conventions using these reserved symbols cause compilation to fail. Pre-existing declarations may carry only `nounwind` and `willreturn`; return/parameter attributes and other function attributes are refused. This prevents ABI changes or optimisation promises such as `memory(none)` from changing or removing the probes. Ordinary application names beginning with `otelc_` or `__cyg_profile_func_` still follow common function selection; these prefixes do not reserve an entire namespace. The test runtime in `tests/fixtures/llvm/hooks.cpp` counts calls only; it is not an OpenTelemetry SDK and does not export metrics or traces.

The pass preserves original catches and destructor cleanup, inserts cleanup for otherwise unprotected throwing calls, and checks the resulting IR. It retains selected function pointers in a Mach-O/ELF inventory section. It refuses an already instrumented module, unsupported exception personalities, Windows exception funclets, `musttail` and coroutine calls. The supported profile is synchronous LLVM 22 on the tested Mach-O/ELF targets. Arbitrary LLVM versions/targets, Rust compiler integration, LTO, non-local `setjmp`/`longjmp` exits, process termination and throwing inline assembly are not qualified.

Clang semantic annotations can be read by the internal pass: `__attribute__((annotate("otelc.instrument")))` and `__attribute__((annotate("otelc.exclude")))`. Unrelated annotations remain unchanged and exclusion wins. The common schema currently has no annotation switch, so the tooling API explicitly disables this internal option. Developers need no annotations for configuration-based selection.

## Evidence and remaining work

`make llvm-check` runs the matched real compiler and enforces at least 80% line coverage independently for the Python component and the native pass. The full `make check` also discovers these tests and enforces both floors. Native profiles, retained library identity and coverage reports are written under `build/llvm-pass*`; Python component coverage is under `build/llvm-python.coverage`.

The ordinary fixtures cover C, C++ exceptions, recursion, threads, `-O0`/`-O2`, Unicode and C++ operator selection, exclusions, annotations and compiler refusals. `examples/llvm/workload.ll` is a separate ordinary IR example with no telemetry calls; stock and instrumented executables must both print `42`. Tests compare actual application stdout/stderr and balanced diagnostic probe counts, and check that original fixture files remain unchanged.

These compiler tests do not establish downstream telemetry receipt, native metric overhead, native lifetime completeness or oteleq language admission. Those checks belong to the remaining native SDK/frontends and equivalent-workload delivery stages in the [rebuild plan](rebuild-plan.md).
