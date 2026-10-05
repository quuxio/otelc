# Related work and compiler feasibility

## Scope

This is a focused record of primary references used for the initial design, checked on 5 October 2026. It is not an exhaustive patent, novelty, or competitor assessment. Compiler-generated probes, buffered function tracing, and native auto-instrumentation already have substantial precedent.

| Project / primitive | Relevant overlap | Implication for otelc |
| --- | --- | --- |
| [Clang function instrumentation](https://clang.llvm.org/docs/ClangCommandLineReference.html#cmdoption-clang-finstrument-functions) | Function-entry/exit callbacks; before/after-inlining options | Reuse for the first timing backend; qualify actual exit behaviour |
| [GCC instrumentation options](https://gcc.gnu.org/onlinedocs/gcc/Instrumentation-Options.html) | Legacy callback ABI and GCC exclusion options | Useful precedent; do not assume GCC switches exist in Clang |
| [LLVM XRay](https://llvm.org/docs/XRay.html) | Compiler instrumentation with runtime activation and tracing infrastructure | Investigate as an optional backend where its target/runtime capabilities match |
| [Microsoft Spoor](https://github.com/microsoft/spoor) | Compiler-instrumented application tracing | Relevant prior art for compiler/runtime separation |
| [SanitizerCoverage](https://clang.llvm.org/docs/SanitizerCoverage.html) | Compiler-generated callbacks at function/block/edge granularity | Useful instrumentation primitive; not a complete paired duration contract |
| [OpenTelemetry Go compile instrumentation](https://github.com/open-telemetry/opentelemetry-go-compile-instrumentation) | Existing `otelc` tool for Go | Supports the general integration direction; use a distinct installed command |
| [OpenTelemetry C++](https://github.com/open-telemetry/opentelemetry-cpp) | Native telemetry API, SDK, and exporter ecosystem | Avoid confusing a compiler probe layer with a replacement SDK |
| [OpenTelemetry eBPF instrumentation](https://github.com/open-telemetry/opentelemetry-ebpf-instrumentation) | External application observability | Different instrumentation and platform tradeoffs; compare capabilities directly |

The proposed product value is the integration of inspectable selection, a bounded native runtime, validated compiler semantics, and OTLP export across tested macOS/Linux lanes. Do not claim that none of these components exists elsewhere or that using LLVM makes the design universally portable.

## Local compiler checks

The following small feasibility fixture ran natively on macOS ARM64. It combines recursive normal returns with a thrown exception caught by the caller. Hooks are built separately without instrumentation; `main` is explicitly uninstrumented.

| Compiler | Flags | Entries | Exits |
| --- | --- | --- | --- |
| Apple Clang 21.0.0 (`clang-2100.3.34.2`) | `-O2 -finstrument-functions` | 6 | 5 |
| Homebrew Clang 21.1.8 | `-O2 -finstrument-functions` | 6 | 5 |
| Homebrew Clang 23.1.2 | `-O2 -finstrument-functions` | 6 | 5 |
| Homebrew Clang 21.1.8 | `-O2 -finstrument-functions-after-inlining` | 2 | 1 |
| Homebrew Clang 23.1.2 | `-O2 -finstrument-functions-after-inlining` | 2 | 1 |

These are observed counts from this fixture, not a complete compiler support result. The missing exceptional exit blocks a balanced-trace claim for these ordinary callbacks. The changed after-inlining counts also show why instrumentation granularity needs explicit tests. The newer installed Clang did not remove the exception limitation.

`hooks.c`:

```c
#include <stdio.h>

static unsigned entries, exits;

void __cyg_profile_func_enter(void *function, void *caller) {
    (void)function;
    (void)caller;
    ++entries;
}

void __cyg_profile_func_exit(void *function, void *caller) {
    (void)function;
    (void)caller;
    ++exits;
}

void report(void) {
    printf("entries=%u exits=%u\n", entries, exits);
}
```

`probe.cpp`:

```cpp
extern "C" void report(void);

__attribute__((noinline)) int recursive(int depth) {
    return depth ? recursive(depth - 1) + 1 : 0;
}

__attribute__((noinline)) void throwing() {
    throw 42;
}

__attribute__((noinline)) void exercise() {
    volatile int result = recursive(3);
    (void)result;
    try {
        throwing();
    } catch (int) {
        // The caller deliberately catches the fixture's exception.
    }
}

__attribute__((no_instrument_function)) int main() {
    exercise();
    report();
}
```

After saving those files into a temporary directory, use the desired compiler explicitly:

```sh
# Apple toolchain; run from the fixture directory.
clang -c hooks.c -o hooks.o
clang++ -O2 -finstrument-functions probe.cpp hooks.o -o probe
./probe

# Homebrew toolchain, using the active macOS SDK.
sdk=$(xcrun --show-sdk-path)
/opt/homebrew/opt/llvm/bin/clang -isysroot "$sdk" -c hooks.c -o hooks.o
/opt/homebrew/opt/llvm/bin/clang++ -isysroot "$sdk" -O2 \
  -finstrument-functions probe.cpp hooks.o -o probe
./probe
```

Homebrew LLVM can coexist with Apple's compiler. Use explicit executable paths and a matched LLVM plugin toolchain rather than replacing the system compiler globally. Installation guidance is maintained in the [official Homebrew formula](https://formulae.brew.sh/formula/llvm).

The fixture is intentionally single-threaded and not a runtime implementation. Its counters and `printf` are unsuitable for production probes. The [roadmap](roadmap.md) defines the broader concurrency, correctness, platform, and performance qualification work.
