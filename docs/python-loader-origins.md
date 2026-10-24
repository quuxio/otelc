# Native Python source-loader origins

<!-- markdownlint-disable MD013 -->

This optional contributor component connects an ordinary source-loader read to the original compiler's actual returned code and the loader's actual return. It shares the [native entry catalogue](python-native-entry.md), with distinct process-local roots, transactions, loader identities and first code executions. Public launches still negotiate journal 1 and report `source_complete=false`. This is not complete Python source/dependency closure or provider admission.

## Explicit capture and validation

Build the optional native image profile, then run its maintained tests:

```sh
.venv/bin/python -m ci.build_python_inputs --origin-entry
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_loader_origins test_python_entry_origins test_python_origin_pins -v
make python-inputs-check
```

The generated driver accepts private `--origin-loader` in place of `--origin-entry`. It enables both native entry and source-loader relations in journal 2. Tests own the inherited descriptor and original application process. This interface does not add application imports, annotations or developer-authored startup code, and does not change the public launcher or format-negotiation API.

## What establishes a source transaction

Only the pinned original frozen `SourceLoader.get_code`, `source_to_code`, `FileLoader.get_data` and `_call_with_frames_removed` route is eligible. The source-loader class, original method resolution, function identities and code identities are checked. A genuine local VM start establishes the particular `get_code` frame. The native read observer supplies both its exact returned immutable bytes object and the actual journal sequence, after the writer releases its lock.

The original builtin compiler object keeps its method definition, identity, equality and hash. A native vectorcall observer accepts only the exact frozen helper stack and that same read-result object. It forwards the literal compiler arguments and records the original returned code weakly. The route requires module execution, `dont_inherit=True` and the actual optimisation argument, with the interpreter configuration resolving `-1`. No post-run compilation, filename match or equal-byte comparison creates an origin.

A genuine local `get_code` return must be that same compiler-returned object. Only then is a route-2 root written and its direct code-valued constants added to the bounded weak catalogue. Existing local start callbacks identify actual executions. Repeated equal reads have separate tokens and transactions; separate loader instances have separate opaque identities. Ordinary workers in the owning interpreter can perform independent transactions.

## Failure, ownership and bounds

Global monitoring events remain disabled. CPython does not permit local `PY_UNWIND`, so compiler errors clear their pending record directly. Other abandoned calls retain no application frame or loader: frame addresses are opaque borrowed identifiers and are never dereferenced. A later genuine start invalidates reused addresses and prunes abandoned records absent from its bounded same-thread live stack. The remaining data are immutable read bytes and weak code references, bounded by 32 pending slots and the journal budget. Cleanup releases only the owned monitor and restores only the compiler vectorcall pointer that this observer installed.

The shared catalogue retains at most 4,096 code and root identities, direct constant paths have at most 128 edges, and loader identities use a bounded weak table. Existing 16 MiB record, 64 MiB journal and record-count limits remain in force. Missing or changed helpers, capture capacity loss, unverified reads or differing compiler/loader returns cannot become complete capture. Original application values, exceptions, output channels and exit status remain the comparison boundary.

Cached/sourceless bytecode, custom loaders, arbitrary dynamic compilation, other interpreters/forks and unsupported native image profiles remain unqualified. A cache hit can read source bytes for hash checking without compiling them; it must not borrow that read as a source-compilation root. Such a successful unsupported loader return produces capture loss. A failed compile has no successful root to publish. Earlier startup reads, installed/native dependencies and complete-program coverage still require separate work.

## Acceptance still required

The native component has an independent 80% line floor. Tests exercise repeated and concurrent original reads, exact descriptors, interpreter optimisation levels, source encoding cookies, equal-byte unrelated compilations, substituted reads/compilers, cache refusal, original syntax errors, weak object/code cleanup, existing builtin dictionary keys and failure before compilation. These tests do not establish universal preservation. Independent review, the full repository gate, required hosted checks, SDK joins, both-lane supervisor integration, public journal-2 negotiation and frozen-provider oteleq qualification remain separate acceptance requirements.
