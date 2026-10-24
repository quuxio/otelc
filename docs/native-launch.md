# Native application launch candidate

<!-- markdownlint-disable MD013 -->

This development profile compiles original C or C++ translation units through LLVM 22, using the common schema-3 policy. Developers add no instrumentation code. Tooling generates a separate startup object and links a shared runtime using the pinned official OpenTelemetry C++ metric/tracing SDK and OTLP/HTTP exporters. The public language plan still keeps C and C++ in `remaining`: automatic object/resource lifetimes, task/service propagation, broader build integration, independent qualification and complete language benchmarks are unfinished.

## Build and run

Install LLVM 22, CMake 3.22+, Ninja, Protobuf with its compiler, and libcurl development headers/libraries. The SDK build fetches the pinned C++ SDK, OpenTelemetry protocol and JSON source archives over verified HTTPS with SHA-256 checks. On Apple Silicon macOS it recognises Homebrew's Protobuf and curl prefixes. The build records its sources, compiler/build-tool identities and output library; this is not complete installed-library dependency closure.

```sh
make native-launch-sdk
```

Create `/tmp/native-demo/main.cpp` with ordinary application code:

```cpp
#include <cstdio>
#include <stdexcept>

int workload(int value) {
  if (value < 0) throw std::runtime_error("original failure");
  return value + 7;
}

int main() {
  int value;
  while (std::scanf("%d", &value) == 1) {
    try {
      std::printf("%d\n", workload(value));
    } catch (const std::exception &error) {
      std::printf("%s\n", error.what());
    }
    std::fflush(stdout);
  }
}
```

Save `/tmp/native-demo.toml`:

```toml
schema_version = 3
languages = ["c", "cpp"]

[resource]
service_name = "otelc-native-demo"

[sources]
include = ["**"]

[functions]
include = ["workload*"]

[metrics]
collection = false
emission = false

[traces]
enabled = true
sample_ratio = 1.0
```

Start the documented [local collection stack](benchmarks.md#run-equivalent-language-comparisons), then launch:

```sh
bin/quux-otelc --config /tmp/native-demo.toml native \
  --language cpp --root /tmp/native-demo --source main.cpp \
  --build /tmp/native-demo-build --runtime /tmp/native-demo-runtime \
  --sdk-library "$PWD/build/native-launch-sdk/libotelc_native_runtime.dylib"
```

On Linux use `libotelc_native_runtime.so`. Build and runtime directories must be new; the build directory must be outside the application root. Repeat `--source` for multiple translation units of the selected language. This profile uses matched `-O2 -g -fexceptions -pthread` compilation in both lanes. `--plain` selects the executable without the bootstrap or SDK runtime. Application arguments follow `--` and are passed literally.

Enter an integer to run the workload. In another terminal enable metrics without restarting or rebuilding the application:

```sh
bin/quux-otelc metrics --socket /tmp/native-demo-runtime/control.json on
bin/quux-otelc metrics --socket /tmp/native-demo-runtime/control.json collection off
bin/quux-otelc metrics --socket /tmp/native-demo-runtime/control.json emission off
bin/quux-otelc metrics --socket /tmp/native-demo-runtime/control.json status
bin/quux-otelc health --socket /tmp/native-demo-runtime/control.json
```

Tracing remains independent of metric switches. Close application stdin to exit, then inspect the retained checkpoint:

```sh
bin/quux-otelc health --report /tmp/native-demo-runtime/health.json
```

The fixed private `agent.log` retains SDK diagnostics separately from application output, capped at 1 MiB. Health reports dropped diagnostic messages. A final `timed_out` checkpoint means the shutdown budget expired; pending exports and queue-loss uncertainty remain visible. The process-owned runtime is retained while any native frame or SDK drain still uses it. A successful SDK exporter call alone does not establish Collector/Prometheus/Tempo receipt.

## Selection and boundaries

The explicit `llvm22-itanium-source-v1` compiler profile requires debug metadata and applies the common slash-aware source globs before function globs. Exported function identities include the project-relative file, for example `main.cpp::workload(int)`. This keeps same-named file-local functions in different translation units distinct. Functions whose source is outside the application root, excluded sources and compiler-generated functions without a supported source identity are omitted. Original source and non-system header identities must match between plain and instrumented compilation and again before launch. This is not a claim of whole-source or native-library closure; `source_complete=false` remains explicit.

The source, SDK library and executable receipts are held by the caller. The launcher refuses altered retained manifests, sources, headers, compiler/plugin inputs or products before either execution lane. The CLI accepts an explicit operator-selected SDK library; its byte identity does not prove that an arbitrary supplied library was built by our CMake recipe.

Original C++ exceptions retain their behaviour and selected unwinds increment error metrics and mark spans as errors. Native error attribution currently uses the generic `exception` label. Tested startup ordering includes global construction before `main` and destruction afterwards. Original priority-zero application initialisers remain refused. Platform-specific static initialisation outside the qualified compiler profile, dynamic image unload, non-local `longjmp`, detached work across process termination, forced termination and applications that independently configure this embedded SDK remain unsupported.

Requested native lifetime or task/HTTP instrumentation fails explicitly before building. General CMake/Make discovery, arbitrary flags, mixed-language compilation recipes, C++ coroutine propagation and Rust are separate remaining work. These limits must be removed by implementation and qualification before the language is considered delivered.

## Focused checks

`make check` includes the actual launcher gate. It builds the pinned SDK library, runs the selected application/tooling tests with no skips, and requires at least 80% line coverage separately for the C++ runtime, Python launcher, gate and suite runner. Linux CI installs Protobuf/compiler and libcurl development packages; macOS CI uses Homebrew Protobuf and curl. Run it independently with:

```sh
make native-launch-check
```

The gate retains compact source/tool/product identities, merged native profiles, exact runtime LCOV and its CMake compiler entry under `build/native-launch*`. `OTELC_CPP_ANALYSIS=1` additionally keeps dependency headers and working directories until Sonar scanning finishes. See [native analysis](native-analysis.md).

For development against an explicitly selected existing library, build it with `python -m otelc.native_launch --output NEW_DIRECTORY --coverage`, then run:

```sh
OTELC_NATIVE_LIBRARY=/absolute/path/libotelc_native_runtime.dylib \
  PYTHONPATH=.:tests .venv/bin/python -m unittest test_native_launch -v
make llvm-check
```

Direct unittest discovery without `OTELC_NATIVE_LIBRARY` skips the launcher integration group; it is not a substitute for `native-launch-check`. The default full gate builds and selects its own library and rejects missing/skipped tests. Passing these component checks is still separate from full language admission.
