# Python selected-origin journal component

<!-- markdownlint-disable MD013 -->

This is a wire-format and reader component for the next source-origin observer. It does not yet produce compiler/loader/VM origin evidence from an application, change `prepared_python.protocols()`, or enable Python provider admission. The normal launcher and its private encoder option still write journal 1. Whole-program source/dependency closure remains unsupported; `source_complete=false` is unchanged.

The intended observer must join an actual read transaction, the pinned compiler's returned root and the selected object's execution. Matching a filename, equal source bytes or a post-run compilation does not establish those relations. Both prepared lanes need the same SDK-independent observer; the plain lane must have no SDK telemetry. These runtime and consumer qualifications remain work.

## Explicit version selection

The C writer accepts version 1 or 2 through `otelc_py_journal_open_version`; its existing `otelc_py_journal_open` entry always selects 1. Invalid versions refuse ownership of the descriptor. Origin kinds are refused, with a capture failure, in version 1. Readers select one closed version explicitly and refuse mixed versions. There is no automatic fallback.

`otelc.python_origin_journal.decode(data, expected_pid=pid)` validates retained version-two bytes. `read(path, expected_pid=pid)` also requires an owned, bounded, non-followed 0600 regular file. The caller must first wait for the owning process and final SDK drain. The reader never compiles, unmarshals or executes retained code.

## Binary contract

Every integer is unsigned big-endian, except the existing signed source timestamp seconds. The 36-byte envelope is `!8sBBHQQQ`: magic `OTLCPY2\0`, kind, zero flags, zero reserved field, consecutive sequence starting at 1, path length and payload length. Paths are nonempty filesystem bytes without NUL, except the empty path on the final record. The existing limits remain: 16 MiB of data per record, 64 MiB per journal, 4096 total records and 65535 bytes per path. Final-record space stays reserved.

| Kind | Metadata | Data following metadata |
| --- | --- | --- |
| 1: native entry input | Existing journal-1 source `!QQQQqQQ` | Exact consumed source bytes |
| 2: final | Existing journal-1 final `!7Q` | None |
| 3: loader input | Existing journal-1 source `!QQQQqQQ` | Exact read bytes |
| 4: SDK-selected code | Existing journal-1 code `!7Q` | Descriptor 2 JSON |
| 5: compiler root | `!10Q`: PID, root ID, transaction ID, loader ID, input sequence, route, Python version, descriptor version, compiler flags, optimisation | Descriptor 2 JSON |
| 6: first origin execution | `!7Q`: PID, origin code ID, root ID, bytecode offset, Python version, constant-edge count, zero reserved field | That many `!Q` indices into successive `co_consts` tuples |
| 7: SDK join | `!7Q`: PID, SDK code ID, origin code ID, bytecode offset, three zero reserved fields | None |

The input token is its actual sequence from the writer's critical section. Two reads of equal bytes have distinct tokens. The event-writing APIs return zero on failure through their token output; callers must not retain an earlier successful token.

IDs are opaque positive process-local integers, never application addresses. Root IDs, transactions and consumed read tokens cannot be reused. Route 1 requires a native entry record and loader ID 0; route 2 requires an ordinary `.py` loader read and a positive loader-instance ID. Source and root paths must agree exactly. A selected cache or sourceless route is outside this initial source-only contract.

The root must reference an earlier actual input and contain a validated module descriptor. Both initial routes compile a module in `exec` mode, with no inherited Python-frame flags; the source-loader route fixes `dont_inherit=True`. The compiler flags are compiler input flags, distinct from the returned object's `co_flags`, and are bounded to a nonnegative 31-bit value. Optimisation is 0, 1 or 2. The production observer must record the actual values, rather than infer them from environment settings or the returned descriptor. Version and filesystem codec must agree across all descriptors in the same process journal.

An execution references an earlier root through at most 128 direct code-valued `co_consts` edges. Each graph member gets one first-execution identity; aliases cannot give it a second identity. Its path, Python version and even bytecode offset must agree with the referenced member. A join references earlier execution and SDK code records, has a unique SDK/origin pairing, and requires their path, offset, Python version and complete bounded code graphs to agree. Graph comparison checks consistency of an already explicit relation; equality never creates a missing relation. Its work budget is shared across the whole journal.

Final counts cover **all** non-final records. The final data-byte count covers source bytes, SDK/root descriptor bytes and execution-edge bytes, excluding each kind's metadata and paths. Loss counts, interpreter finalisation and native-entry expectation retain their journal-1 meanings. `capture_complete` reports only final raw capture health; it does not imply that every requested function is bound, an application succeeded, telemetry arrived, or a provider is qualified.

## Validation and remaining work

Reader tests use real descriptors as fixture data and falsify replayed/missing/forward references, invalid graph edges, mismatched SDK descriptors, foreign PIDs, mixed versions, unsupported routes, malformed framing, truncation and terminal counters. Native writer tests cover actual per-kind widths, counts, failed/cleared event tokens, concurrent reads and version-1 refusal. Fixture records are not evidence of a genuine compiler observation.

Run the focused tests and the native component gate:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_origin_journal test_python_origin_writer test_python_input_journal -v
make python-inputs-check
```

The reader has an explicit 80% line floor in `make test`, in addition to the existing overall Python and per-file native gates. Actual native entry, live loader transactions, pinned compiler returns, bounded weak code membership, both-lane supervisor wiring and independent oteleq admission still need implementation and qualification. Custom loaders, arbitrary dynamic compilation and whole-program closure remain separate capabilities.

## Pre-startup helper guard

The explicit native `--origin-observer` profile retains the original frozen `SourceLoader.get_code`, `SourceLoader.source_to_code`, `FileLoader.get_data`, `_call_with_frames_removed` and builtin compiler before application startup hooks run. It retains the four Python functions' original code objects too: changing a function's `__code__` preserves its function identity and must not pass that guard. Native dictionary scans accept only exact Unicode keys and compare their text without hashing or application equality/attribute callbacks, including interpreter module lookups. The guard preserves an exception already in flight and refuses a fork or another interpreter before dereferencing the owning interpreter's references.

After ordinary interpreter startup, missing or changed helpers produce capture loss while the application continues with its original startup hooks, outputs and exceptions. The normal profile does not install this guard. The retained references are bounded toolchain objects, not application code or a code-object origin catalogue.

This guard is a prerequisite for the future live transaction observer. It emits no origin records and proves no compiler return, input-to-code relation, descendant membership or selected execution. The driver still writes journal 1, normal public format negotiation is unchanged, and Python remains a candidate. The native guard has its own 80% line floor alongside the other native components.
