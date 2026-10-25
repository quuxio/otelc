# Config-only C++ object lifetime candidate

<!-- markdownlint-disable MD013 -->

The explicit native build-4 candidate connects the common schema-3 policy to Clang construction discovery, LLVM probes, the official C++ SDK and owned execution. Developers add no annotations, imports, guard members or bootstrap. Original application files stay unchanged. This is a tested candidate; independent review, protected delivery, oteleq provider qualification, lifetime performance comparisons and Grafana demonstration remain required before whole-language admission.

## Build and run the example

The ordinary [example source](../examples/native/lifetimes.cpp) constructs one `demo::Item` on each successful workload call. Input `2` returns `7`; input `-1` reports `caught -1`. Failed construction creates no lifetime observation. The class contains no telemetry code:

```cpp
struct Item {
  int value;
  explicit Item(int input) : value(input) {
    if (input < 0) throw input;
  }
  ~Item() {}
};

int workload(int input) {
  Item local(input);
  return local.value * 3 + 1;
}
```

The [complete common policy](../examples/native-lifetimes.toml) selects `*workload*` function names and `demo::Item` lifetime types. Both metric switches start off; tracing starts on. From the repository root, with LLVM 22, its Clang development libraries, CMake, Ninja and the documented [native SDK dependencies](native-launch.md) installed:

```sh
make native-launch-sdk
bin/quux-otelc native-protocols --build-version 4
bin/quux-otelc --config examples/native-lifetimes.toml native-build \
  --language cpp --root examples/native --source lifetimes.cpp \
  --output build/native-object-example --sdk-build build/native-launch-sdk
make stack-up
bin/quux-otelc --config examples/native-lifetimes.toml native-run \
  --build build/native-object-example --runtime build/native-object-run
```

Enter integers at the application prompt. In another terminal, enable metrics in the same application process and then enter more integers:

```sh
bin/quux-otelc metrics --socket build/native-object-run/app/control.json on
bin/quux-otelc metrics --socket build/native-object-run/app/control.json collection off
bin/quux-otelc metrics --socket build/native-object-run/app/control.json emission off
bin/quux-otelc metrics --socket build/native-object-run/app/control.json status
bin/quux-otelc health --socket build/native-object-run/app/control.json
```

Collection and emission are independent. Emission-off retains collected measurements for later export; collection-off suppresses new measurements. Neither switch stops lifetime accounting or enabled traces. The live bound is supplied by common `lifetimes.max_live`, and refused capacity is visible as loss. Type and source selections are build-time decisions and require a new build if changed.

Close stdin with Ctrl-D to finish. Inspect final `object_accounting` through:

```sh
bin/quux-otelc health --report build/native-object-run/app/health.json
```

Run the same compiled plain lane with `native-run --plain` and a new runtime directory. Every build and runtime directory must be new. The [stack guide](benchmarks.md#run-equivalent-language-comparisons) explains collection ports and dashboard access. Lifetime spans use names such as `demo::Item lifetime`; the Collector sends them to Tempo, while lifetime metrics reach Prometheus. A successful local SDK export alone does not prove downstream storage or dashboard receipt.

## Observed boundaries and build context

Successful complete-constructor return starts an observation. Complete-destructor entry ends its lifetime interval; destructor exit separately records escaped exceptions. Constructor failures create no observation. Shutdown records still-live objects as `shutdown_censored`, never as destroyed. Lifetime spans are roots with a link to a selected creation function context when one exists. They do not require function selection, and their creation links do not establish task/service propagation.

The tested boundary includes ordinary locals, heap objects, arrays, copy/delegating construction, member initialisers shared by several constructors, global and thread-local initialisers, cross-thread destruction and throwing cleanup. Actual Clang ABI symbols and IR call graphs bind generated storage initialisers; a guessed function name is insufficient. Initial global construction can have no selected creation context.

`--source` supplies the ordinary C++17/O2/exceptions-enabled recipe. `--recipe` accepts the same explicit [per-unit build contexts](native-build-context.md), including mixed C/C++, private VFS inputs and an uninstrumented observation driver. Each application C++ unit gets its own context-bound semantic inventory using its real flags and working directory. C units retain function probes and acquire no C++ object probes. Generated observer functions marked with Clang's profiling-exclusion attribute acquire no lifetime creations.

Build 4 uses source-build schema 4, native metadata schema 3/profile `llvm22-native-object-units-v1`, inventory schema 2 and object ABI `otelc.object.v1`. Protocol discovery explicitly returns those additions; default discovery remains build 2 and function-only explicit recipes remain build 3. Preparation rechecks semantic/private/header inputs, complete compiler channels and products without invoking a compiler. It reconstructs selected facts from retained Clang output, so deleting construction claims and rehashing only the inventory does not authorise a launch. This remains input/product evidence, with `source_complete=false` and `exec_verified=false`.

## Checks and remaining scope

```sh
make native-public-lifetimes-check
make native-lifetimes-check
make native-check
```

The public check uses real plain/instrumented applications, owned preparation/release, unchanged source, direct decoded OTLP, all four metric states, type exclusions, lifetime-only selection, tracing disabled, live capacity and censored shutdown. It also checks mixed per-unit/VFS contexts and refusals for changed selection, headers and semantic claims. The new Python builder has its own 80% coverage floor; the Clang helper and LLVM object pass retain their separate native floors. `OTELC_PUBLIC_OBJECT_SDK` can select an already-built matching SDK for this focused check.

Trivial/aggregate construction is refused by this profile. Materialisation-only boundaries, implicit storage reuse without a destructor, Microsoft C++ ABI, C resource-close pairing, native task/service propagation, arbitrary flags and automatic build-system discovery remain unsupported. The older immediate `native` command still refuses lifetime policies; use `native-build` plus `native-run`. Broader object/language support and independent generated/retained oteleq qualification remain open.
