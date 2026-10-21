# Java synchronous action protocol

<!-- markdownlint-disable MD013 -->

This is the optional `java-sync-action-v1` diagnostic producer protocol. Use it with a frozen SDK source/product snapshot and [the supported action shapes](action-witness.md). It does not replace the common instrumentation policy, change the default prepared-launch or runtime-health schemas, or establish downstream receipt.

## Prepare and connect

`otelc.witness.compile_bindings(application, declarations, output)` runs the offline Java compiler using the extension and its standalone `-helper-tool.jar` ASM dependency. `application` is a validated `PreparedApplication`; `declarations` has exactly `envelope`, `rendezvous`, `wrapper` and `target`, each with `owner` (internal JVM name), `name` and `descriptor`. `output` must not exist. The compiler reads original classes without loading them and checks application input identity before and after compilation.

`prepared_java.prepare(..., witness_bindings=output)` checks the common policy, bindings and original class hashes, then writes `witness.properties` and `witness.json` in the fresh owned 0700 runtime directory. The returned launch identity keeps its existing 16 fields; its tooling list binds these optional files as well. The metadata names the separate `witness.sock` and final `witness-health.json`. Both runtime and receiver confirmation are mandatory. The profile requires tracing enabled with ratio 1 and the target selected by policy; it cannot be requested in the plain lane.

The socket is owned by the launch user and mode 0600. Use `otelc.witness.request(socket, operation, *arguments)` for correlated, bounded exchanges. Requests are ASCII, a single LF-terminated line, at most 2048 bytes before LF:

```text
1 REQUEST_ID status
1 REQUEST_ID status RUNTIME_ID SCOPE_ID
1 REQUEST_ID arm RUNTIME_ID COOKIE SEQUENCE CASE_ID ATTEMPT_ID BUILD SELECTION TOOLING WRAPPER_PROBE POLICY
1 REQUEST_ID end RUNTIME_ID SCOPE_ID
1 REQUEST_ID cancel RUNTIME_ID SCOPE_ID
```

`REQUEST_ID` is a fresh 32-character lowercase hexadecimal value chosen by the client. Runtime, cookie and scope IDs are 32-character lowercase hexadecimal; case, attempt, digest and probe IDs are 64-character lowercase hexadecimal. Sequence and counters use canonical non-negative decimal strings; sequence starts at 1. Case/attempt identity is client-supplied, not an application value. All arm identities must match the current ready envelope and frozen launch metadata. The server accepts no application values or arbitrary executable commands.

Wait for the ordinary fixture to reach its rendezvous, then read `status` and require a ready scope and no producer error. Send `arm` with that exact cookie/sequence and launch identities. Require the correlated successful `armed` acknowledgement before releasing the ordinary fixture channel. Observe consumed/exited state, require the application outcome, then call `end`. Cancellation records a sticky failure; it does not stop or alter application execution. `end` waits at most 1500 ms for a real SDK flush outside the scope monitor. A delayed export can settle and be checked with another `end`.

## Closed response

Every response is LF-terminated JSON with exactly these header fields. Unknown or missing fields, duplicate JSON keys, wrong types and a wrong request ID are rejected by the Python client.

| Field | Type and meaning |
| --- | --- |
| `schema_version`, `profile` | Integer `1`, string `java-sync-action-v1` |
| `runtime_id`, `request_id` | Runtime identity; echoed request identity, or null for an invalid uncorrelated request/final header |
| `error` | Null or a bounded lowercase error code |
| `build_sha256`, `selection_sha256`, `policy_sha256`, `tooling_sha256` | Frozen launch identities |
| `wrapper_probe_id` | Exact wrapper binding identity |
| `rejected_probes` | Decimal-string rejected-entry counter |
| `max_scopes`, `max_spans_per_scope` | Integers `64` and `1024` |
| `scope` | Null or the closed object below |

| Scope field | Type and meaning |
| --- | --- |
| `cookie`, `sequence` | Fresh live envelope cookie and monotonic decimal-string sequence |
| `scope_id`, `case_id`, `attempt_id` | Null before arm; opaque identities after arm |
| `phase` | `entered`, `ready`, `armed`, `consumed`, `exited`, `expired`, `cancelled`, `complete`, `invalidated`, `incomplete` or `terminated` |
| `failure` | Null or first retained failure code |
| `trace_id`, `root_span_id`, `target_span_id` | Actual observed SDK identities, null until observed |
| `root_exited`, `envelope_exited`, `target_exited` | Booleans from actual role exit probes |
| `target_entries` | Decimal-string count, including supported recursion |
| `export_failures`, `flush_failures`, `pending_exports` | Decimal-string counts; earlier failures are retained after recovery |
| `checkpoint`, `producer_complete` | Booleans; producer completion requires the evidence below |
| `receiver_confirmation_required` | Always true |
| `started_ids`, `ended_ids`, `exported_ids` | Unique observed span-ID arrays, each at most 1024 entries |
| `queue_dropped` | Null until a supported shutdown drain; then a decimal-string count of ended tracked spans with no export attempt |

Producer completion requires a successful flush checkpoint, no sticky failure, root/envelope/target exits, at least one target entry, matching non-empty started/ended/exported sets containing root and target, and no pending export or known queue drop. The client rejects contradictory completion claims. Exporter success means the SDK exporter reported success; independently decoded receipt must match the actual trace/span IDs and expected parent relationships.

The wrapper and target spans carry `otelc.witness.role` and their exact `otelc.witness.probe_id`. All observed child spans in that trace carry `otelc.witness.scope_id`, `runtime_id`, `context_cookie`, `case_id`, `attempt_id`, `build_sha256`, `selection_sha256`, `policy_sha256` and `tooling_sha256` under the same `otelc.witness.` prefix. Recursion uses role `recursive_target`. A separate lifetime trace with a link is not counted as an action child.

The runtime serialises one active action. Other envelopes and unrelated reflected/re-entrant entries cannot borrow its token. Waiting contexts expire after 30 seconds before consumption; an already-running action is not declared complete or expired by an invented execution deadline. A tagged span after the checkpoint invalidates it. Capacity, missing target/probe, class identity change and absent final evidence cannot become successful fallback results.

## Final evidence

Read the post-exit report with `otelc.witness.read_final(path)`. The reader requires an owned private directory, a non-symlink regular private file and a maximum size of 4 MiB. The closed document has `schema_version: 1`, `kind: otelc.java-witness-final`, `phase: final`, positive integer `pid`, `producer` (the header above with null request/scope), `queue_evidence` and `scopes`. Scopes retain their original order, unique cookies and unique non-null scope IDs, with at most 64 entries. Incomplete contexts remain present.

`queue_evidence` is `derived_after_shutdown` only when the existing runtime-health boundary proves the supported global drain; otherwise it is `unavailable` and per-scope queue loss remains null. A failed attempted export is distinct from a queue drop. Abrupt termination can leave no report: that is missing evidence, not a completed action. Retain the final report, default runtime-health report, launch/tooling identities, application outcome and independently decoded receiver data together.
