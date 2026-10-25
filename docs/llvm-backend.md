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

The record binds the compiler/plugin source, owned ABI header, compiler identities, actual compile command, compilation database, diagnostics and resulting library. The Python API retains an immutable caller-held build result; an edited on-disk record does not replace it. The [native analysis pipeline](native-analysis.md) uses that actual build context for C++ scanning. This is a local component binding, not a signature or whole-program provenance claim. `source_complete=false` stays explicit.

## Explicit function registry

The default `llvm22-itanium-v1` profile keeps probe ABI1: `otelc_function_enter_v1` and `otelc_function_leave_v1`. A separately built `llvm22-itanium-registered-v1` profile uses probe ABI2 and additionally requires `otelc_function_register_v1`. Select it explicitly:

```sh
.venv/bin/python -m otelc.llvm_backend --function-registry --output build/llvm-registered
```

The Python build API takes `function_registry=True`. Its immutable manifest identifies the distinct profile and ABI; there is no implicit registration upgrade or fallback. Both variants use the same common function selectors and exception-aware probes.

The compiler emits a read-only array of selected function addresses and NUL-terminated UTF-8 demangled names, using the C-compatible descriptor in `native/llvm/function_abi.h`. It generates a priority-zero module constructor that passes this array and its count to the registration callback. Ordinary application initialisers can then call selected functions without authoring registration or bootstrap code. Original priority-zero initialisers are refused in this profile because their ordering could precede registration. Empty selection emits no registration table, constructor or additional link dependency.

Tooling-supplied callbacks must honour the header's non-throwing ABI and retain only data valid for the loaded image lifetime. The existing calling-convention, definition, return/parameter and function-attribute checks apply to the new callback. Function registration describes compiler selection; it does not prove a function ran. Native runtime/export/control wiring, object/resource lifetimes, dynamic-image unload and broader source/build profiles remain required work.

## Explicit source-aware profile

`--source-aware` builds `llvm22-itanium-source-v1`, using the same registered ABI2 descriptor layout. The Python API takes `source_aware=True, function_registry=True` and `environment(policy, language, source_root=canonical_project_root)`. Compile application translation units with `-g`; missing module debug metadata is refused. Default and name-only registered profiles retain their existing behaviour.

The source profile applies common `[sources]` include/exclude patterns before function selection. Path `*` and `?` do not cross `/`; `**` does, and `**/` also matches zero directories. Unicode and literal punctuation retain the common policy semantics. Definitions must resolve to physical files inside the canonical application root; a project symlink to an external header does not bring that header into selection. Missing or compiler-generated source identities are omitted. Debug paths are not an attestation of source origin; complete compiler-input closure remains unsupported.

Registered names become `relative/file.cpp::demangled_function_signature`. Two translation units' same-named local functions therefore have distinct telemetry identities. Native SDK frontends can consume this profile; runtime metric flags and task/lifetime behaviour remain separate.

## Common selection and probe contract

`otelc.llvm_backend.environment(policy, 'c')` or `'cpp'` translates the common `[functions] include/exclude` lists into the compiler environment. `*` matches zero or more characters, `?` matches one Unicode character, and C++ punctuation such as `[]` is literal. Exclusion wins. C++ selection uses demangled function signatures. Metric switches do not change which calls the compiler inserts; the runtime applies those switches live.

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

This is tooling code, not code developers add to an application. A frontend can pass the returned library to Clang's `-fpass-plugin=...` option with that environment. The explicit source profile adds source-file selection; general build-system interception remains frontend work. Passing every source file to either older profile does not enforce `[sources]` selection.

The inserted C ABI is:

```c
uint64_t otelc_function_enter_v1(const void *function);
void otelc_function_leave_v1(uint64_t token, int32_t unwinding);
```

Probes must return normally, must not throw, and must preserve application behaviour. `unwinding` is zero for a normal return and one when an Itanium exception leaves the selected function. The entry token is passed to the matching exit. Existing application definitions, globals, incompatible types or non-C calling conventions using these reserved symbols cause compilation to fail. Pre-existing declarations may carry only `nounwind` and `willreturn`; return/parameter attributes and other function attributes are refused. This prevents ABI changes or optimisation promises such as `memory(none)` from changing or removing the probes. Ordinary application names beginning with `otelc_` or `__cyg_profile_func_` still follow common function selection; these prefixes do not reserve an entire namespace. The test runtime in `tests/fixtures/llvm/hooks.cpp` counts calls only; it is not an OpenTelemetry SDK and does not export metrics or traces.

The pass preserves original catches and destructor cleanup, inserts cleanup for otherwise unprotected throwing calls, and checks the resulting IR. It retains selected function pointers in a Mach-O/ELF inventory section. It refuses an already instrumented module, unsupported exception personalities, Windows exception funclets, `musttail` and coroutine calls. The supported profile is synchronous LLVM 22 on the tested Mach-O/ELF targets. Arbitrary LLVM versions/targets, Rust compiler integration, LTO, non-local `setjmp`/`longjmp` exits, process termination and throwing inline assembly are not qualified.

Clang semantic annotations can be read by the internal pass: `__attribute__((annotate("otelc.instrument")))` and `__attribute__((annotate("otelc.exclude")))`. Unrelated annotations remain unchanged and exclusion wins. The common schema currently has no annotation switch, so the tooling API explicitly disables this internal option. Developers need no annotations for configuration-based selection.

The pass also honours LLVM's `noprofile` function attribute. Clang emits it for `no_profile_instrument_function`, so private observation helpers can stay outside an include-all policy without enabling optional annotation reading. See the [native build-context guide](native-build-context.md#observation-tooling-inputs); application developers do not add these helpers or attributes.

## Evidence and remaining work

`make llvm-check` runs the matched real compiler and enforces at least 80% line coverage independently for the Python component and each native pass variant. The registered variant repeats the base compiler corpus and additionally checks selected address/name agreement, UTF-8 and demangled C++ names, exclusions, ordinary pre-main initialisers, empty selection and incompatible registration declarations. The full `make check` also discovers these tests and enforces the floors. Default artifacts use `build/llvm-pass*`; registered artifacts use `build/llvm-pass-registry*` and a separately retained plugin. Python component coverage is under `build/llvm-python.coverage`. C++ Sonar uses the source-aware compilation context, which includes the base, registration and source-selection implementations.

The source-aware group checks duplicate file-local function names, standard-library exclusion, physical project boundaries including external symlink targets, Unicode/path-glob agreement, missing debug metadata, original exceptions and annotation exclusion. Its artifacts use `build/llvm-pass-source*`.

The ordinary fixtures cover C, C++ exceptions, recursion, threads, `-O0`/`-O2`, Unicode and C++ operator selection, exclusions, annotations and compiler refusals. `examples/llvm/workload.ll` is a separate ordinary IR example with no telemetry calls; stock and instrumented executables must both print `42`. Tests compare actual application stdout/stderr and balanced diagnostic probe counts, and check that original fixture files remain unchanged.

These compiler tests do not establish downstream telemetry receipt, native metric overhead, native lifetime completeness or oteleq language admission. Those checks belong to the remaining native SDK/frontends and equivalent-workload delivery stages in the [rebuild plan](rebuild-plan.md).
