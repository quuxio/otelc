# Native per-unit build context

<!-- markdownlint-disable MD013 -->

Status: candidate build-3 component. This accepts explicit C/C++ compilation contexts and a separate neutral observation driver without editing application source. The ordinary schema-3 TOML policy still selects instrumentation and live metric controls. The recipe describes an existing build, not another instrumentation policy. Automatic compilation-database/build-system discovery, generated oteleq provider qualification and task/service propagation remain work. The separate [build-4 candidate](native-public-lifetimes.md) adds C++ lifetimes through these same unit recipes.

## Select the format

```sh
bin/quux-otelc native-protocols --build-version 3
```

This returns `native-owned-v1` with `build: 3` and `unit_recipe: 1`. Preparation, command, release and execution records retain version 1. Default discovery and function-only `native-build --source` keep build 2. Enabled C++ lifetime policies explicitly select build 4. A consumer must explicitly negotiate the new recipe before using it.

For the ordinary application and common policy in the [native launch guide](native-launch.md), a minimal `/tmp/native-context.json` is:

```json
{
  "schema_version": 1,
  "kind": "otelc.native.unit-recipe",
  "assets": [],
  "units": [{
    "id": "main",
    "role": "application",
    "source": "/tmp/native-demo/main.cpp",
    "language": "cpp",
    "directory": "/tmp/native-demo",
    "flags": ["-std=c++17", "-O2", "-pthread"],
    "overlay": null,
    "rename_main": false
  }],
  "link": {
    "language": "cpp",
    "directory": "/tmp/native-demo",
    "flags": ["-pthread"]
  }
}
```

Each unit retains its original compiler flag order and working directory. Both lanes receive the same flags and an added `-g`, which supplies the source-aware pass's required debug metadata. Source/output/dependency switches and the instrumentation plugin belong to otelc. Supported semantic options include the declared C/C++ standards, optimisation, warning/layout flags, split `-D`/`-U`, and split `-I`, `-isystem`, `-iquote`, `-include` and `-imacros`. Response files and unknown compiler flags are refused. Link flags currently accept `-pthread` and named system libraries such as `-lm`; arbitrary link scripts and library closure are not supported.

Paths are canonical and absolute, except relative path-option values, which retain their per-unit working-directory meaning. Application units and their working directories stay inside the application root. IDs are distinct ordinary identifiers; `link` is reserved for compiler-channel bookkeeping. Explicit variants of one source can have distinct IDs and flags. Runtime function names retain the existing source/function identity; they do not add a separate variant metric label.

The public `--language` selects the native application adapter and its resource label. Each unit's `language` separately selects its C or C++ compiler, so a C++ target can include C source units. All declared application units follow that target's common function/source policy; the language list selects adapters rather than acting as a per-file exclusion rule. Use common source/function exclusions to omit application code.

Build and run with the same prepared-launch commands:

```sh
make native-launch-sdk
bin/quux-otelc --config /tmp/native-demo.toml native-build \
  --language cpp --root /tmp/native-demo --recipe /tmp/native-context.json \
  --output /tmp/native-context-build --sdk-build build/native-launch-sdk
bin/quux-otelc --config /tmp/native-demo.toml native-run \
  --build /tmp/native-context-build --runtime /tmp/native-context-run
```

Use `--plain` for the matched lane without SDK probes. Use a new runtime directory for every launch. The [prepared-launch guide](native-prepared.md) explains external host release and the unchanged standard streams, PID ownership, final health and live control commands. Runtime preparation does not execute recorded compiler commands.

## Observation tooling inputs

Qualification tools can supply one additional unit with `role: "driver"`. Its source must be a declared private asset outside the application root. The driver is compiled without the instrumentation plugin in both lanes, including when the common function policy includes `**`. Developers do not author that driver or add SDK imports to their applications.

Set `rename_main: true` on an application unit when an independently generated driver supplies `main`. The matched `llvm-objcopy` renames `main`/`_main` to the fixed observation entry symbol; its identity, command and complete output channels are retained. No arbitrary post-build command is accepted.

An application unit's optional `overlay` names a declared private JSON VFS asset. This profile accepts version 0, `use-external-names: false` and ordinary file roots mapping original application paths to separately declared private contents outside the application root. Original source and private helper identities remain distinct. The original files are not replaced on disk.

Generated observation accessors inside a private overlay use Clang's [profiling-exclusion attribute](https://clang.llvm.org/docs/AttributeReference.html#no-profile-instrument-function):

```cpp
extern "C" __attribute__((no_profile_instrument_function))
int observed_state() { return state; }
```

The LLVM pass honours the resulting `noprofile` semantic attribute independently of optional annotation reading. This is tool-generated code, not an annotation developers add to application source. An ordinary `no_instrument_function` declaration does not provide that IR marker in the tested Clang setup.

## Evidence and limits

The build retains complete compile/rename/link argv and working directories, stdout/stderr, per-unit dependency files, a compilation database, both executable lanes and source/private/tool/product identities. Per-unit dependency output uses `-MD`, including headers reached through `-isystem` and ordinary compiler system include paths. Changed application system headers must be refused as inputs, rather than disappearing from the denominator. This adds header identity/read costs during build and verification. Preparation rechecks the full declared input and compiler-channel denominator. It does not execute metadata-selected commands. A caller-held build or prepared request cannot be repaired by rehashing changed records or inputs. This is not compiler-origin attestation for an independently fabricated build. `source_complete=false` and `exec_verified=false` keep their existing meanings; installed-library and complete compiler-input origin proof remain separate.

```sh
make native-units-check
```

The focused check builds a real SDK unless `OTELC_NATIVE_SDK_BUILD` selects a matching existing build. Tests use two application units with different directories, defines and standards; actual C and C++ driver lanes; a private C++ overlay; main rename; original exceptions; all four metric states in the same application; direct OTLP receipt and pre-constructor OS ownership. Changed headers, helpers and compiler records are refused, even if their old bytes are restored with new file facts. The Python component has its own 80% floor. Full project checks, independent review, protected CI and actual downstream provider qualification remain required before language admission.
