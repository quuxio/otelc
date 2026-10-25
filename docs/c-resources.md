# Automatic C allocation lifetimes

<!-- markdownlint-disable MD013 -->

Status: public launch candidate. Source-build 5 connects Clang-resolved C record-pointer allocations and the late LLVM probe pass to the official SDK pipeline. `native-build` and `native-run` use the common lifetime policy, preserve developer source and keep live collection/emission independent. Independent review, full exact-head checks, protected delivery and oteleq qualification remain required; this is not complete C language admission.

## Expected behaviour and acceptance

The common schema-3 policy selects C record-pointer allocations by the type resolved by Clang. Developers keep their original source and add no telemetry calls, imports, annotations or bootstrap. The first profile observes ordinary `malloc` and `calloc` returns through matched LLVM probes and the existing official C++ SDK pipeline. The unchanged shared policy applies to the C example as well as the other language examples.

- A successful, non-null allocation starts one bounded observation. A failed allocation starts none and preserves the original result and `errno`.
- The first `free` entry claims that address generation; completion is published only after the original call returns. `free(NULL)` and untracked frees do not create observations or false losses. A `void *` alias must not erase an existing tracked allocation's identity.
- Reused addresses and cross-thread release keep distinct generation tokens. Observers never read allocated storage, add object fields or own application allocations.
- Metrics and spans use `boundary="free_entry"`, distinct from C++ `destructor_entry`. These are allocation-to-release intervals, not proof of C++ construction or the lifetime of every array element.
- Collection and emission retain independent live controls, including startup-off to on. Metric switches do not suppress enabled lifetime spans.
- Capacity refusals, duplicate admission and invalid completion remain visible. Shutdown censors unfinished allocations without inventing a successful free.
- The compiler must bind actual C types, call sites, source selection and per-unit flags. Both lanes preserve original source/configuration and process channels; changed inputs or unsupported selected operations must refuse.
- Real SDK, compiler, public CLI, decoded OTLP and benchmark checks need at least 80% coverage for every new component, independent review and protected exact-head checks before delivery. Passing runtime tests alone does not enable the capability.

The initial compiler profile will not cover `realloc`, custom or interposed allocators, stack scope boundaries or arbitrary pointer-flow inference. Those remain separate work. A `free` boundary does not establish external file/socket/resource release.

## Compiler boundaries

LLVM can remove allocations and frees during optimisation. The candidate validates source/type/call bindings before optimisation, retains an immutable string plan in the module and inserts probes after optimisation. Inlining can produce several emitted calls for one source site; an optimiser can also replace a `malloc`/zero-fill pair with `calloc`. The exclusive per-unit resolution record retains actual emitted call counts and callee names. A zero-match source site produces no fabricated lifetime. These counts describe static compiler boundaries, not runtime invocation counts or complete allocation coverage.

`make native-resources-check` runs the real Clang/LLVM tests and independent 80% native/Python coverage floors. The tests compare stock and instrumented outcomes for retained allocations, allocator elision, inlining, allocator-family replacement and unoptimised builds. They also refuse changed, incomplete or ambiguous source bindings, conflicting callback ABIs and an existing resolution output. Compiler-only tests use test-owned observation callbacks. The separate public tests use the actual SDK, owned prepared execution, both CLI lanes, successful/failed allocation, `errno`, decoded OTLP, all four live metric states and censored shutdown. Private per-unit headers, VFS helpers and a neutral C++ caller have a distinct fixture.

## Runtime checks

`make native-check` exercises actual allocation/free and deterministic address reuse, type-erased and cross-thread release, all four initial metric states, startup enable, creation links, pending release and censored shutdown. It also runs the existing C++ destruction, exception cleanup and SDK control checks. Each native implementation retains its own coverage floor.

## Run the ordinary C example

The [C example](../examples/c/BenchmarkApp.c) uses an ordinary heap-allocated `WorkItem` with a volatile pointer binding so optimisation retains its allocation. This is an example workload choice; developers do not need annotations or source changes for existing applications. Elided allocations remain unobserved. Both stock and instrumented builds consume exactly the same source.

```sh
make native-launch-sdk
make stack-up
run_root=$(mktemp -d /tmp/otelc-c-resources.XXXXXX)
bin/quux-otelc --config examples/otelc.toml native-build \
  --language c --root "$PWD" --source examples/c/BenchmarkApp.c \
  --sdk-build build/native-launch-sdk --output "$run_root/build"
printf '1000\nquit\n' | bin/quux-otelc --config examples/otelc.toml native-run \
  --build "$run_root/build" --runtime "$run_root/runtime"
```

Source-build 5 negotiates `unit_recipe=1`, `resource_inventory=1`, `resource_resolution=1` and `resource_probe_abi="otelc.resource.v1"`. Older function-only and C++ object profiles keep their own versioned contracts. The SDK records C `resource_accounting` with `releasing`, separately from C++ destruction accounting. Resource callbacks preserve application `errno`; untracked/null release creates no lifetime. The health checkpoint still cannot prove downstream storage or complete source origin.

Use `<runtime>/app/control.json` with the shared `metrics` command for live collection/emission changes. See [native benchmarks](native-benchmarks.md) for stock/four-state comparisons and [Grafana](benchmarks.md) for dashboards. Compare `free_entry` durations only with compatible allocation/release boundaries; C++ destruction and GC observations have different meanings.

```sh
make native-public-resources-check
make benchmark BENCHMARK_LANGUAGE=c BENCHMARK_CONFIG=examples/otelc.toml
```
