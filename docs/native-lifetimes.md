# C++ lifetime semantics component

<!-- markdownlint-disable MD013 -->

Status: development component. This inventories actual C++ types and construction sites using the installed LLVM 22 Clang AST, type and USR APIs. It is preparation for automatic object lifetime probes; it does **not** emit lifetime spans, establish successful construction or observe destruction. Native lifetime instrumentation remains work.

Developers do not add annotations, imports, guard members or bootstrap code. Original application and header bytes are checked before and after inspection. No application is executed, and object layout is unchanged. One schema-3 policy supplies source and lifetime type selections:

```toml
schema_version = 3
languages = ["cpp"]

[sources]
include = ["**"]
exclude = ["vendor/**"]

[lifetimes]
enabled = true
include = ["demo::WorkItem", "demo::BoxItem<*>"]
```

The internal Python API is `otelc.native_lifetimes.inspect(policy, root, sources, output)`. `policy` is the ordinary `Policy.load(config_path)` result; `sources` contains paths relative to the original application root. `output` must be a new directory outside that root. A caller can build one helper with `build_tool(new_directory)` and pass its returned `Tool` to repeated inspections. Each use verifies that caller-held identity, the fixed helper source, its headers, shared Clang/LLVM libraries and matched compiler tools. An edited on-disk receipt cannot replace the caller-held build result.

The helper resolves alias chains, specialised templates, local and anonymous types, const objects, arrays, aggregates and out-of-line delegated constructors through Clang. Each selected construction records the canonical type USR, actual selected constructor symbol/signature, complete/base/delegating kind and physical source position. Variable materialisation is recorded separately, including NRVO candidates and initialisers that do not contain a constructor expression. Macro expansion positions retain physical source coordinates; `#line` coordinates are separate debug fields. This distinction is necessary for a later source-to-IR join.

Receipts retain compact semantic JSON compressed with deterministic gzip, input file identities and literal compiler arguments. The full compiler AST is not retained. Failed compiler queries, changed dependencies, changed helper products, absent canonical bindings and unsupported target profiles fail inspection without publishing a successful inventory. `source_complete=false` stays explicit.

## Run the component checks

Install the matched LLVM 22 compiler and development libraries. Homebrew's `llvm@22` supplies these on macOS; the maintained Ubuntu installer also installs `libclang-22-dev` and `libclang-cpp22-dev`.

```sh
make native-lifetimes-check
```

The tests compile and run the real Clang helper against unchanged C++ fixtures. They require at least 80% Python and native helper line coverage. Native coverage and build context are included in the shared Sonar pipeline. `OTELC_CPP_ANALYSIS=1` retains the exact helper binary and raw profiles for coverage replay.

## Remaining work

The next stage must join these semantic records to compiler probes and the native SDK runtime. A lifetime must start only after successful construction. Base/delegating constructor variants must not double-count a complete object, and address reuse must produce separate observations. Destruction, explicit resource release and observations censored at shutdown need distinct boundaries. Exception unwinding, copy/move, arrays, NRVO, placement construction and cross-thread destruction require executed plain-versus-instrumented evidence. This inventory alone does not satisfy those requirements or qualify C++ as complete.

The initial compilation recipe is C++17 on Itanium macOS/Linux targets. Arbitrary build flags, general build systems, Microsoft C++ ABI, foreign standard libraries and compiler plugins are not covered. A metadata-only Linux-target test on macOS does not establish Linux application execution.
