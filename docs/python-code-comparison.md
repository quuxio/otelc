# Retained Python code comparisons

<!-- markdownlint-disable MD013 -->

The post-run diagnostic API compares selected code descriptors with the exact source and whole-pyc bytes retained in the native journal. It does not reopen application source paths. It reports each selected code ID as `unique-match`, `ambiguous`, `unmatched` or `incomplete`. `source_complete` remains false. A semantic match does not establish which compiler or loader invocation produced the original code object.

This is a component API, not automatic public-launcher integration or rebuilt-provider oteleq admission. Invoke it only after the owning application and SDK have stopped. The journal reader requires final ordered framing, the actual application PID, final counts, private-file checks and valid code descriptors. These checks do not independently establish process liveness, SDK health or collector delivery.

## Use retained inputs

For tooling running from this checkout with the locked Python environment, use the original request's interpreter file identity and the actual application PID from the final execution record:

```python
from pathlib import Path
from otelc.python_code_evidence import inspect
from otelc.python_launch_files import read

runtime = Path('/absolute/path/to/completed/runtime')
request, _ = read(runtime / 'python-request.json')
execution, _ = read(runtime / 'python-execution.json')
result = inspect(
    runtime / 'python-inputs.journal',
    expected_pid=execution['application']['pid'],
    interpreter=request['interpreter']['file'],
    flags=0,
    optimize=0,
)
```

Compilation flags and optimisation are explicit diagnostic inputs. Set them to the actual application's compiler profile; this API does not infer arbitrary `compile()` calls, inherited future flags or environment options. The example uses the ordinary unoptimised script profile. Compiler future flags never leak from the tooling module because the worker uses `dont_inherit=True`. Source encodings, BOM and line endings remain in the retained bytes.

Each source input is compiled in a separate `-I -S -B` worker. Target modules are never imported or executed. A pyc worker requires the selected interpreter's magic, recognised header flags and exactly one marshalled code payload with no trailing bytes. Timestamp, checked-hash and unchecked-hash headers are retained with `pyc_validation=not-checked`: this diagnostic does not validate cache freshness or reproduce importlib's invalid-cache fallback decision. A stale unchecked-hash cache may match an observed descriptor while current source does not.

The worker records the input hash, exact interpreter file identity, worker/encoder source identities, fixed argv, terminal exit and descriptor hash. Identity changes, compiler refusal, malformed pyc, worker crashes and timeouts remain incomplete. It has CPU and output-file limits and an owning-process timeout. This is process isolation, not an operating-system memory sandbox or a safe decoder for arbitrary unauthenticated marshal data. Retain only inputs from the owned application evidence boundary. The [marshal documentation](https://docs.python.org/3.14/library/marshal.html) describes its format/version and input restrictions; [PEP 552](https://peps.python.org/pep-0552/) defines the cache header modes.

Graph comparison preserves metadata, bytecode, filename/name text, constants, float/complex bits and shared references. Snapshot-local IDs and envelope offsets do not define code values. Frozenset order is ignored, but a later alias can require backtracking over an earlier unordered choice. Structural hashes filter candidates; exact values and a bidirectional node mapping establish a match. Exhausted work or branch-depth budgets produce incomplete results. One overall comparison budget and deadline checks limit a journal inspection; a long individual graph comparison is bounded by its work budget rather than interrupted by the wall clock.

## Remaining boundary

The diagnostic tries nested code roots from every retained entry/import input. Equal candidates remain ambiguous, and a refused candidate can hide an additional match, so it prevents a unique result. No selected descriptors gives `no-selected-code`, rather than a successful coverage claim. Failed capture/finalisation remains incomplete. Original application exits, SDK health and collector evidence are not changed by this result.

Pyc filename repair, precise source/cache selection, dynamic AST/replaced code, custom loaders, mixed filesystem/version profiles, whole-program source coverage, loaded-library/dependency closure and automatic supervisor publication remain separate work. The API retains pyc filenames as stored; it does not imitate importlib's filename repair. The [native journal guide](python-input-journal.md) describes the capture boundary.

Focused checks:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_code_evidence test_python_code_candidate \
  test_python_code_comparison test_python_code_worker \
  test_python_code_index -v
make check
```
