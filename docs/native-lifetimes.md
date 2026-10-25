# C++ lifetime semantics component

<!-- markdownlint-disable MD013 -->

Status: development component. This inventories actual C++ types and construction sites using the installed LLVM 22 Clang AST, type and USR APIs. Inspection does not itself emit lifetime spans, establish successful construction or observe destruction. The separate [object probe component](native-object-probes.md) consumes these records in compiler-linked internal SDK tests; public native lifetime launch remains work.

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

The internal Python API is `otelc.native_lifetimes.inspect(policy, root, sources, output)`. Default calls retain inventory schema 1 and the fixed C++17 recipe. The explicit compiler-context component below uses schema 2. `policy` is the ordinary `Policy.load(config_path)` result; `sources` contains paths relative to the original application root. `output` must be a new directory outside that root. A caller can build one helper with `build_tool(new_directory)` and pass its returned `Tool` to repeated inspections. Each use verifies that caller-held identity, the fixed helper source, its headers, shared Clang/LLVM libraries and matched compiler tools. An edited on-disk receipt cannot replace the caller-held build result.

The helper resolves alias chains, specialised templates, local and anonymous types, const objects, arrays, aggregates and out-of-line delegated constructors through Clang. Each selected construction records the canonical type USR, actual selected constructor symbol/signature, complete/base/delegating kind and physical source position. Variable materialisation is recorded separately, including NRVO candidates and initialisers that do not contain a constructor expression. Macro expansion positions retain physical source coordinates; `#line` coordinates are separate debug fields. This distinction is necessary for a later source-to-IR join.

Receipts retain compact semantic JSON compressed with deterministic gzip, input file identities and literal compiler arguments. The full compiler AST is not retained. Failed compiler queries, changed dependencies, changed helper products, absent canonical bindings and unsupported target profiles fail inspection without publishing a successful inventory. `source_complete=false` stays explicit.

## Run the component checks

Install the matched LLVM 22 compiler and development libraries. Homebrew's `llvm@22` supplies these on macOS; the maintained Ubuntu installer also installs `libclang-22-dev` and `libclang-cpp22-dev`.

```sh
make native-lifetimes-check
```

The tests compile and run the real Clang helper against unchanged C++ fixtures. They require at least 80% Python and native helper line coverage. Native coverage and build context are included in the shared Sonar pipeline. `OTELC_CPP_ANALYSIS=1` retains the exact helper binary and raw profiles for coverage replay.

## Remaining work

The separate probe component joins supported non-trivial constructor/destructor records to the native SDK. General materialisation and resource-release boundaries, public policy/bootstrap wiring, downstream receipt and benchmark/qualification evidence remain work. This inventory alone does not establish source completeness or qualify C++ as complete.

The initial compilation recipe is C++17 on Itanium macOS/Linux targets. Arbitrary build flags, general build systems, Microsoft C++ ABI, foreign standard libraries and compiler plugins are not covered. A metadata-only Linux-target test on macOS does not establish Linux application execution.

## Explicit compiler contexts

The internal `inspect(..., context=context)` form inventories one C++ unit with its actual working directory, ordered semantic flags and optional private VFS overlay:

```python
context = {
    "directory": "/absolute/application/unit",
    "flags": ["-std=c++20", "-O1", "-D", "SCALE=5", "-I", "../include"],
    "overlay": None,
    "assets": [],
}
inventory = inspect(policy, root, [Path("unit/context.cpp")], output,
                    tool=held_tool, context=context)
```

This emits schema 2 with profile `clang22-cxx-context-v1`, the copied `compile_context`, private `context_inputs`, actual commands and original application root. It reuses the native unit recipe's closed flag/path/overlay rules. One call accepts one unit so variants of the same source have separate, unambiguous inventories. The default schema-1 API and recipe are unchanged.

Relative includes and defines retain their compiler-directory meaning. The helper's compilation database uses that directory, while source/type selection still uses the original application root. Its file manager receives the same private VFS filesystem as the compiler; merely retaining an overlay flag does not establish that it read the overlay. Original source and private overlay identities remain distinct, and both are rechecked before publication. Header dependencies include system headers.

The maintained compiler tests cover C++20, optimisation-dependent preprocessing, ordered defines, relative includes, original source bytes and actual private overlay type declarations. They retain the failing default-recipe and wrong-overlay observations. This is an inventory component. Public lifetime build/preparation, schema-2 inventory consumption by the object pass, downstream launch integration and language qualification remain work.
