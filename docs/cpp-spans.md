# C++ function-body spans

The matched LLVM adapter instruments unchanged C++ application function bodies, including supported Itanium exception exits. Qualification is macOS ARM64 with Clang/LLVM 22 and the locked Rust toolchain. It uses the same private SDK store, producer records, independent exporter and coherent loss handling as [C spans](c-spans.md).

## Run an unchanged application

```sh
make build
make stack-up
mkdir -p build/tutorial
./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp doctor
./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp clang++ \
  -O2 -g -std=c++17 examples/apps/cpp-traces.cpp -o build/tutorial/cpp-traces
./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp inspect \
  build/tutorial/cpp-traces
OTELC_REPORT_PATH=build/tutorial/cpp-traces-report.json \
  ./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp run \
  build/tutorial/cpp-traces
```

The [source example](../examples/apps/cpp-traces.cpp) contains ordinary constructors, destructors and a catch; it has no instrumentation imports, attributes, members or manual probes. Configuration selects `build_order`, `choose_value`, `Order::*` and `Base::*`. It tests normal and failed construction, delegation, base cleanup and a separate thread. Original source, object size, exception payload identity, output and cleanup counts match the plain build.

A constructor span begins at its compiled function entry, including the calls that initialise bases and members. A failed constructor gets escaping-unwind status; already-constructed base cleanup remains a normal destructor invocation. A caller that catches the exception and returns normally keeps unset status. Destructors executed during another function's unwind are normal unless an exception escapes their own invocation. Arguments and exception messages are not exported.

## Understand ABI entry points

Clang can emit distinct complete-object and base-object constructor/destructor bodies with the same demangled name. They can call each other. These are distinct selected function-body invocations, so both appear in the tree and contribute to function metrics. This does not imply two object lifetimes or two executions of a user destructor's statements.

When selected bodies have different C++ linkage names but the same demangled name, telemetry and inspection labels append the linkage identity, for example:

```text
Order::Order(int) [linkage=_ZN5OrderC1Ei]
Order::Order(int) [linkage=_ZN5OrderC2Ei]
```

Inspection also prints their original configuration name. Selection and exclusion continue to match the original demangled name, such as `Order::Order(int)`, and select its compiled bodies. Existing manifests without this optional selection name remain readable by the updated tools. A newly generated manifest containing it requires the matching updated CLI/runtime. Truly ambiguous names with the same linkage identity, and selected aliases/folded functions sharing an address, still fail visibly.

These spans measure functions. They do not establish automatic object lifetime support, implicit/trivial construction coverage, or one span per object instance. The [automatic lifetime requirement](design.md#source-free-instrumentation-contract) remains separate.

## Existing annotations

The same configuration reads [the annotated C++ example](../examples/apps/annotated.cpp):

```cpp
__attribute__((annotate("otelc.instrument")))
int throw_order(int value) {
    if (value < 0) throw std::runtime_error("invalid order");
    return value;
}
```

```sh
./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp clang++ \
  -O2 -g -std=c++17 examples/apps/annotated.cpp -o build/tutorial/cpp-annotated-traces
./target/debug/quux-otelc --config examples/cpp-traces.toml --language cpp run \
  build/tutorial/cpp-annotated-traces
```

It produces nine selected invocations: four `process_order`, four configured calls and one escaping `throw_order`. Configuration exclusions and `otelc.exclude` override annotation inclusion. Existing annotations are optional; users do not add them to enable instrumentation.

## Controls, losses and viewing

The nearest selected same-thread invocation is the parent; other threads start independent roots. Sampling is inherited by the whole tree. Metrics admission can change live and stays fixed for an already-entered call, while trace enablement and sampling remain static. Native admission/stack/queue loss, SDK capacity loss, missing exits and thread retirement discard affected trees. Both native and trace loss counters matter.

Use service `otelc-cpp-traces` in [Grafana traces](http://localhost:3000/d/otelc-traces). The [C guide](c-spans.md#export-controls-and-shutdown) explains shared shutdown budgets, strict typed acknowledgements, separate signal settings, null contended snapshots and unfinished delivery. The [stack guide](observability-stack.md) documents the Collector, Tempo, Prometheus and Grafana.

Callbacks, other exception personalities, Windows funclets, coroutines, LTO, sanitizers, shared libraries and task/distributed/manual SDK parenting are unsupported by this qualification. `noexcept` termination, fatal signals and abort paths have no guaranteed flush. Additional native platforms need qualification. The existing benchmarks measure function metrics; tracing overhead requires a separate workload comparison and has no established production acceptance threshold.
