# Automatic C allocation lifetimes

<!-- markdownlint-disable MD013 -->

Status: development. The SDK runtime now has a separate `free_entry` boundary with type-erased release lookup. The compiler and public C build/launch route are not connected yet; the current CLI still refuses enabled C lifetimes. This is not C language admission.

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

## Runtime checks

`make native-check` exercises actual allocation/free and deterministic address reuse, type-erased and cross-thread release, all four initial metric states, startup enable, creation links, pending release and censored shutdown. It also runs the existing C++ destruction, exception cleanup and SDK control checks. Each native implementation retains its own coverage floor.
