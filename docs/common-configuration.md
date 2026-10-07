# Common configuration for every language adapter

## Implemented contract and current support

Use one TOML document with `schema_version = 2` for C, C++, Rust, TypeScript, JavaScript, Java, Python and Go. [`examples/common.toml`](../examples/common.toml) is a validated example. The shared Rust configuration library implements parsing, defaults, validation and per-language resolution. The native C/C++ compiler wrapper and runtime consume this schema now; Python consumes the resolved policy through its monitoring adapter; JavaScript/TypeScript use generated in-memory compiler input, Java uses bytecode probes, Go uses compiler overlays and Rust uses generated source plus a Cargo wrapper. A valid policy does not imply that every requested language or feature is executable.

All adapters must consume this common policy, either through the shared library or the CLI's resolved JSON. They must preserve its selection, precedence, limits and telemetry semantics rather than introduce independent policy files. A language-specific backend may need additional typed settings as it is implemented; those settings belong under `adapters.<language>` and must be validated by the common schema.

Schema 1 remains accepted for the existing native fixtures, including the interim manual lifetime guard. Schema 2 expresses automatic lifetime intent and never substitutes the guard for source-free instrumentation. See the [source-free requirement](design.md#source-free-instrumentation-contract).

## Shared policy and adapter settings

| Section | Meaning shared by every adapter |
| --- | --- |
| `languages` | Enabled target IDs: `c`, `cpp`, `rust`, `typescript`, `javascript`, `java`, `python`, `go` |
| `sources` | Original application paths to include/exclude, before generated-file rewriting |
| `functions` | Qualified display names to include/exclude |
| `annotations` | Optional reading of existing metadata and injection into generated input |
| `lifetimes` | Automatic type/resource selection and the requested lifetime boundary |
| `runtime` | Maximum admitted functions/live lifetimes and shutdown deadline |
| `metrics` | Enablement and duration histogram boundaries in seconds |
| `traces` | Trace enablement, root sampling ratio and bounded trace sizes |
| `export` | OTLP protocol, base destination, batching, timeout and queue limit |
| `resource` | Service identity and bounded resource attributes |
| `adapters.<language>` | Backend preference and applicable implementation settings |

The same selection policy, service identity, histogram buckets and Collector destination therefore apply to every resolved language. Backend choices do not change the meaning of those fields.

Paths use `/` and match relative to the project working directory; invoke adapters from that directory. `sources` uses path globs, including `**` across directories. Function/type/resource name patterns use only `*` and `?` as wildcards; other punctuation is literal. Names remain the language adapter's qualified display names, so a shared file may contain patterns for several naming conventions. A common format does not imply that Java packages and C++ namespaces have identical spellings. Adapters must expose their names in inspection output.

Resolved matcher expressions operate on UTF-8 bytes and use dot-all matching, as the native glob compiler does. Adapters must preserve those options, including the `(?-u)` byte-mode prefix, rather than applying Unicode-character wildcards or treating newlines differently.

Exclusions always win. Empty includes supply no configuration opt-ins; supported annotation opt-ins can still select functions when reading is enabled. Instrumentation code and SDK/runtime dependencies must remain protected from self-instrumentation. Existing annotation interpretation is optional and requires an adapter that implements it; exclusions cannot be overridden by annotation metadata. Generated annotations must not modify original files.

`lifetimes.boundary` is `object`, `resource` or `collection`. `object` requires language-defined initialisation/destruction or drop boundaries, `resource` requires observable existing close/dispose operations, and `collection` requests allocation-to-collection measurement where supported. These are distinct intervals. An adapter must report an unsupported boundary; it must not manufacture a destructor or silently require manual application probes.

## Resolve and validate

```sh
make build
./target/debug/quux-otelc --config examples/common.toml --language cpp config
./target/debug/quux-otelc --config examples/common.toml --language java config --json
# Require support for every feature requested for this selected language:
./target/debug/quux-otelc --config examples/common.toml --language cpp config --require-supported
```

`config` validates the entire document and resolves one enabled language. It exits successfully for a valid future policy, reporting unavailable capabilities. `--require-supported` returns a failure when the selected adapter or requested features are not implemented. `--json` emits a fully defaulted schema-2 plan with the selected language/backend, shared policy, applicable native settings, complete `metrics_endpoint`, `execution_available` and `unavailable` reasons. It contains no authentication headers. `source_matchers`, `function_matchers` and `lifetime_matchers` carry the authoritative glob compiler's byte-oriented regexes for adapters, preserving wildcard and exclusion semantics.

`execution_available` describes static implementation support for the configured requests. It does not probe installed compiler tools, the application or Collector connectivity; use `doctor` and native/pipeline validation for those checks. All eight languages resolve from the same example, but C/C++, Python, JavaScript, TypeScript, Java and Go function timing can execute currently.

Rust, Python, Java and JavaScript support function spans with `traces.enabled=true`; other language adapters still reject tracing explicitly. See [Rust spans](rust-spans.md) for root sampling, whole-tree buffering, capacity loss and independent signal export settings. See [Python spans](python-spans.md), [Java spans](java-spans.md) and [JavaScript spans](javascript-spans.md) for their parenting and execution boundaries. Resolved trace-capable plans include `trace_export` only when tracing is enabled.

Defaults include empty source/function selections, annotation processing off, automatic lifetimes off, metrics on and traces off. Shared limits are 4096 selected functions, 4096 active calls, 4096 live lifetimes and a 2000 ms shutdown deadline. `runtime.max_active_calls` bounds outstanding observations across all language adapters, including suspended generators and coroutines. Native admission enforces this limit across all threads as well as the per-thread stack limit. Capacity rejection produces an `active_call_capacity` loss and an available slot can be reused after completion or thread retirement. Native defaults are 128 admitted threads, depth 256 and 4096 queue slots per thread. Unknown keys, duplicate/empty language lists, adapter sections for disabled languages, invalid backend choices, inappropriate native settings and invalid capacity/transport/resource values fail validation.

The default backend preferences are LLVM for C/C++, compiler integration for Rust, source processing for TypeScript, a loader for JavaScript, an agent for Java, a monitoring/profile adapter for Python and compile-time integration for Go. They select the implemented function-metrics backends; they do not install dependencies automatically. The example selects the implemented Java agent. AspectJ remains an unavailable alternative.

## Use the same file with native C/C++

```sh
mkdir -p build/common
./target/debug/quux-otelc --config examples/common.toml clang -O2 -g \
  tests/fixtures/timing.c -o build/common/timing
./target/debug/quux-otelc --config examples/common.toml clang++ -O2 -g -std=c++20 \
  tests/fixtures/exceptions.cpp -o build/common/exceptions
./target/debug/quux-otelc --config examples/common.toml --language c run ./build/common/timing
./target/debug/quux-otelc --config examples/common.toml --language cpp run ./build/common/exceptions
```

Compilation infers C/C++ from the driver and source inputs. An explicit `--language` must agree with that invocation. `doctor` and `run` need a selected language for schema 2, because a file can contain different adapter settings. Direct execution uses `OTELC_CONFIG` plus `OTELC_LANGUAGE`; `run` supplies the selected language to the runtime. Existing schema-1 commands continue to work without a language option.

With LLVM, `annotations.read_existing` reads `otelc.instrument` / `otelc.exclude` function metadata. Existing annotations are optional; opt-outs and external exclusions win. `runtime.control_socket` opts into live metrics admission control. Both capabilities require LLVM; callbacks reject them. Enabling `annotations.inject_generated`, `lifetimes.enabled`, tracing or the native `source` backend currently blocks native execution with an explicit unsupported-feature error. The configuration fields and validation are implemented; their source-injection/lifetime/trace execution remains [roadmap work](roadmap.md). C++ function timing uses the matched LLVM backend and preserves exception behaviour without application edits.

## Environment and adapter conformance

Precedence is defaults, TOML and documented OpenTelemetry environment overrides. Resource/service identity and metric export overrides follow the existing [OTLP settings](configuration.md#otlp-settings-and-secrets). A generic endpoint is a base URL; a metrics-specific endpoint is a complete URL. The resolved `metrics_endpoint` includes the final path exactly once, so adapters must use it directly. OTLP headers remain external and are never serialized into the resolved plan or manifests. Environment overrides cannot change selection or silently choose a different backend.

Adapter conformance requires the same defaults, wildcard/exclusion rules, environment precedence, bounds, unsupported-feature errors and resolved policy for shared test vectors. It also requires unchanged-source checks, correct selected-function telemetry, declared lifetime boundaries and paired benchmarks. The implemented tests resolve all eight targets from one file, reject invalid/unsupported requests and run both native C and exception-enabled C++ through schema 2 with an actual OTLP decoder.

See the [developer 101](developer-101.md) for complete unchanged/annotated sources, validated commands, live controls and paired latency measurements. Control sockets are disabled by default; their parent directory must be private and owned by the process user.

The Java agent consumes the same trace policy and independent transport settings for bounded [method-body spans](java-spans.md). Existing bytecode annotations remain optional; a returned asynchronous stage is timed only until the method returns.

JavaScript consumes the common trace policy through its in-memory loader, with private direct-call/await contexts and an independent trace phase. See [JavaScript spans](javascript-spans.md); TypeScript tracing remains unavailable pending its qualification PR.
