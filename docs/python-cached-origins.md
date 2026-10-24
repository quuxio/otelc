# Native Python cached-loader origins

<!-- markdownlint-disable MD013 -->

This experimental contributor component adds a distinct deserialisation route for an existing ordinary source loader's `.pyc` input. It does not change the public `python-native-origins-v2` profile, enable cached origins in that profile, or establish Python language admission. Developers do not edit the target source, delete caches, force recompilation or add telemetry imports. Public prepared execution, live-control integration and independent provider qualification for this component remain work.

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

Maintained cases cover timestamp, checked-hash and unchecked-hash caches, optimisation levels 0/1/2, invalid/stale headers, original marshal errors, repeated warm calls, worker imports, weak cleanup, existing builtin dictionary keys, substituted helpers/marshal calls, unrelated equal cache loads and explicit wire-version refusals. Internal SDK fixtures check actual cached function code IDs on main and worker spans and metric points; their external bootstrap is test scaffolding, not required target application code. Final native 80% component floors, full repository validation, independent review, public integration and protected hosted checks remain required before this component can be delivered.
