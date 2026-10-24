# Native Python entry-origin component

<!-- markdownlint-disable MD013 -->

This explicit contributor profile observes a native source-file entry, the compiler's actual returned module and first VM execution of its code descendants. It adds no application imports, annotations, guards or bootstrap. It is separate from the public Python launcher, which still negotiates journal 1 and reports `source_complete=false`.

The supported image profile is shared/framework GIL CPython 3.12 or 3.14 on macOS ARM64, with the declared native call path and an inspectable thin Mach-O image. An unsupported image, unavailable mapping or missing tools cannot silently qualify another route. LLVM/Linux, other interpreter versions, universal images, module/command entries and bytecode origins need separate support. A separate optional [source-loader component](python-loader-origins.md) shares this entry profile and weak catalogue.

## Explicit build and capture

```sh
.venv/bin/python -m ci.build_python_inputs --origin-entry
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_origin_chain_build test_python_entry_origins test_python_origin_pins -v
make python-inputs-check
```

The builder records the library and inspection-tool hashes, loaded-image UUID, declared adjacent call sites and generated-header identity in a separate immutable product namespace. Normal builds do not derive this optional map. `OriginChain.h` is checked alongside the executable, shared library and encoder header before a cached product is reused. Prepared tooling also retains the new builder source.

The built native driver accepts the private `--origin-entry` option before its inherited journal descriptor and writes journal 2. The older `--origin-observer` option remains the journal-1 helper guard. These are contributor interfaces, not additional application setup steps or public format negotiation. The maintained tests own the file descriptors and original processes; an application developer does not implement this harness.

## What establishes the relation

The native entry snapshot obtains its actual journal sequence from the writer's critical section. The entry observer retains that particular token. A source root is accepted only through the pinned native compiler-return emission and the complete adjacent native file-entry chain. Checking only a filename, equal descriptor, public audit event or a matching outer frame would accept unrelated compilations.

Before using the map, the runtime checks the canonical loaded image path and its in-memory Mach-O UUID. This complements build-time file/tool hashes; it is not an attestation against arbitrary native memory rewriting or foreign runtime interposition. The checked file path starts with zero compiler input flags, while future bits merged during compilation remain in the returned code descriptor. Optimisation must match the actual interpreter configuration.

The observer records the returned module through the private native descriptor encoder. Its bounded catalogue holds weak code references and actual direct `co_consts` edges. Local `PY_START` callbacks check the executing frame, exact code object, offset and `RESUME` instruction before recording an execution. Each first observation disables that local event location. Global monitoring events are never enabled. Descendants can first execute on ordinary workers in the owning interpreter.

The observer reserves a free monitoring ID and retains the original monitoring APIs and disable sentinel before application startup. Failed setup, replaced helpers, capacity loss or invalid observations remain capture failures. Cleanup checks its tool-name identity before releasing a slot. The original application continues with its own values, errors, byte channels and exits; missing origin evidence cannot become successful admission.

## Validation and limits

Tests run genuine native entries, worker calls and original optimisation, encoding, exception and exit cases. Native C controls emit public/private events, compile equal bytes twice and execute a nested file under the same filename. Their independent counters prove that the alternative paths ran. Python callback controls cannot turn an unexecuted descendant into a genuine VM start. Image parser tests reject malformed or ambiguous mappings and UUID metadata. Every compiled native component retains its 80% line floor; an unavailable backend is tested as unavailable.

Root and execution records are read using the existing [journal-2 reader](python-origin-journal.md). The optional [source-loader profile](python-loader-origins.md) adds its separate read/compiler/return transactions. The separate [SDK join component](python-origin-sdk-joins.md) binds selected code identities; complete source/dependency closure, frozen-provider oteleq admission and broader closure remain work. The explicit [public origin profile](python-prepared-origins.md) supplies journal-2 negotiation and both-lane execution. Normal instrumentation, metrics and benchmark commands remain on their existing profile until that integration is qualified. GC observation and resource release retain their existing, distinct lifetime meanings.
