# Native Python cached-loader origins

<!-- markdownlint-disable MD013 -->

The explicit `python-native-cache-origins-v3` candidate captures ordinary source-loader reads from source or an existing `.pyc` cache. It connects native deserialisation evidence to public prepared execution, selected function metrics/spans and live controls. Developers do not edit target source, delete caches, force recompilation or add telemetry imports. The default input profile and explicit source-only `python-native-origins-v2` profile keep their existing contracts. Independent provider qualification and protected delivery remain required; this is not whole-language admission.

## Run the shared example with an existing cache

Use shared/framework GIL CPython 3.12 or 3.14 on macOS ARM64 with the locked SDK environment and a supported native image. Start the [local collector and Grafana](benchmarks.md), then run from the checkout:

```sh
# Create an ordinary cache to demonstrate that it can remain in place.
.venv/bin/python -m py_compile examples/python/BenchmarkApp.py
runtime_parent=$(mktemp -d)
printf '3\nthrow\nquit\n' | bin/quux-otelc --config examples/otelc.toml python \
  --root "$PWD" --entry examples/python/CachedBenchmarkApp.py \
  --runtime "$runtime_parent/runtime" --profile python-native-cache-origins-v3
```

`CachedBenchmarkApp.py` contains an ordinary `import BenchmarkApp`. It runs the existing application, which makes three workload calls and reports checksum `12598976`. The `throw` input reports the original negative-seed `ValueError`. The wrapper adds no instrumentation; an existing application can keep its own entry and imports. Precompilation above is only to demonstrate cache reuse, not a launch requirement. Existing source and cache bytes stay unchanged.

For interactive controls, omit the `printf` pipe and use another terminal while the application waits for input:

```sh
bin/quux-otelc metrics --socket "$runtime_parent/runtime/control.json" collection off
bin/quux-otelc metrics --socket "$runtime_parent/runtime/control.json" emission off
bin/quux-otelc metrics --socket "$runtime_parent/runtime/control.json" collection on
bin/quux-otelc metrics --socket "$runtime_parent/runtime/control.json" emission on
```

Each switch changes the running application and can be used independently. Tracing remains enabled when metric collection or emission is off. Enabling from an initially disabled policy also works without restarting. Enter `quit` to drain the SDK and retain the final reports. These lifetime observations preserve the existing distinction between resource release and observed GC; cached code does not change those semantics.

## Prepare through the public API

Tooling can use `otelc.prepared_python.prepare(..., profile='python-native-cache-origins-v3')`, `command` and `run`. `--prepare-only` exposes the same fixed supervisor recipe through the CLI without starting an application or SDK. `plain=True`/`--plain` uses the same native observer without an SDK or telemetry. That diagnostic lane still has observer overhead; use the benchmark driver's direct stock lane for a performance baseline.

Explicit `protocols(profile='python-native-cache-origins-v3')` returns request, plan, prepared, command and execution formats 3, journal 3 with kinds 1-7, code descriptor 2, health 2 and SDK 1. Its `origin_scope` is `native-entry-and-application-root-source-and-cache-loaders`. `code_comparisons` is null because the separate post-run comparison API accepts journal 1. `source_complete` remains false.

The resolved application root selects `--origin-loader-caches-root`. The prepared manifest binds the maintained producer and its actual native products. After owned shutdown and SDK drain, `python-execution.json` retains independently checked entry identity, root scope, raw capture health and SDK code joins. The plain lane must contain no SDK codes or joins. A substituted loader can leave the application's original output/exit unchanged while causing the tooling result and entry-evidence phase to fail. A complete summary describes this bounded capture, not complete dependency closure or downstream receipt.

## Explicit component capture

Build the native origin-enabled tool and run its maintained cases:

```sh
.venv/bin/python -m ci.build_python_inputs --origin-entry
PYTHONPATH=tests .venv/bin/python -m unittest test_python_cached_origins -v
make python-inputs-check
```

The private driver accepts `--origin-loader-caches-root /absolute/application/root` or `--origin-loader-caches`. The rooted form retains the existing canonical application-source boundary and leaves outside loaders alone. These switches use journal **3**, with the `OTLCPY3\0` marker. `python_origin_journal.read(path, expected_pid=pid, version=3)` must be explicit; the default reader remains journal 2 and refuses journal 3. No format autodetection changes a caller's contract.

## Actual input-to-code relation

Only the pinned original `SourceLoader.get_code`, `_compile_bytecode`, `marshal.loads` and `_imp._fix_co_filename` route is eligible. The existing raw read observer supplies the exact immutable cache bytes object and its actual read sequence. The unmarshaler must receive a one-dimensional contiguous read-only memory view backed by that same object, starting exactly after the 16-byte header and covering the remaining payload. A separate equal bytes object, writable buffer, shifted base or substituted helper cannot supply the relation.

The original marshal builtin keeps its identity, method definition, hash and equality. A local monitoring CALL event on the frozen `_compile_bytecode` helper prevents warm specialised builtin calls from bypassing the native vectorcall observer. No global events are enabled for this purpose. The observer forwards the literal original call and holds its returned code weakly. A genuine loader return must be that same code after the pinned filename-fix route. Existing first-execution and SDK code-ID joins then use the same weak catalogue. Cancellation restores only the marshal dispatch pointer and monitoring events owned by the observer.

A cache root uses route **3**. Its `input_sequence` references the actual earlier `.pyc` read; its framed descriptor path is the actual returned code filename. Those paths are distinct facts and are not equated. The raw read carries its own path and byte identity. Cache roots have compiler-flags field `0` and optimisation field **3**, meaning **unknown/not a compiler invocation**. The observer does not derive a compiler level or original source compilation from the cache filename. Source compiler routes 1/2 keep their existing meaning, including when CPython rejects a cache and falls back to source.

## Bounds, failures and limits

The existing 32 pending loader slots, weak loader/code catalogue, 4,096 roots/code identities, 128 constant edges, 16 MiB record and 64 MiB journal budgets remain. Pending slots retain immutable read bytes and weak returned code; they do not retain an application frame or loader. Native code from forks, subinterpreters, custom/sourceless loaders and other native image profiles remains outside this contributor profile. The selected entry remains a source-file entry. A cache containing a non-module code object cannot become a module origin. An original marshal exception stays an application exception and supplies no successful root.

The reader validates framing, reference roles, code graphs and format identity as data. A fabricated transcript is not authenticated by those checks. Actual producer qualification must establish the native call/return relation and account for selected code in both lanes. `source_complete=false` remains the project-wide declaration; complete source/dependency closure and per-invocation attribution are separate requirements.

## Acceptance

Maintained native cases cover timestamp, checked-hash and unchecked-hash caches, optimisation levels 0/1/2, invalid/stale headers, original marshal errors, repeated warm calls, worker imports, weak cleanup, existing builtin dictionary keys, substituted helpers/marshal calls, unrelated equal cache loads and explicit wire-version refusals. Public prepared cases cover unchanged source/cache bytes in both lanes, actual decoded code IDs, all four live metric states in one PID, startup-off enable, literal CLI application arguments, refused profile downgrades and the shared example's checksum/errors/lifetimes. Native SDK fixtures additionally check worker spans and metric points. Independent provider qualification, final full validation and protected hosted checks remain required before delivery.

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_python_cached_origins test_python_prepared_cached_origins \
  test_python_prepared_origins test_python_recipe test_python_provider_protocol
make check
```

CLI-option and disabled-image tests run independently of supported-origin image tests. A native startup-failure fixture forces only the preceding entry/loader readiness stages, then checks the real failed cache setup, both cancellation calls, original application output and incomplete journal evidence. Its counters use the unchanged production NativeMain translation unit. This component fault test does not establish a usable source-origin backend on an unsupported image.
