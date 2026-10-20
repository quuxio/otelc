# Shared trace-context carrier

The Rust export crate and Python adapter implement the same bounded wire parser and emitter. The carrier alone is a prerequisite for service propagation. The [Python standard-library HTTP integration](python-http.md) uses it for client/server spans when `propagation.http=true`; other languages still require their own qualified transport integrations.

## Contract

The profile uses the [published W3C Trace Context grammar](https://www.w3.org/TR/2021/REC-trace-context-1-20211123/) and preserves the random-trace-ID flag from the [Level 2 draft](https://www.w3.org/TR/trace-context-2/), matching the pinned SDKs. This is not a claim of full Level 2 conformance. The JSON vectors in `tests/fixtures/trace-context.json` are the common language-neutral expectations; future language implementations must consume them before advertising carrier support.

Callers collect every occurrence of each case-insensitive HTTP field name and pass ordered value sequences. They must not silently select one duplicate `traceparent`. Extraction returns no context for a missing, duplicate or malformed parent, uppercase hex, version `ff`, or a zero trace/span ID. Surrounding HTTP space/tab whitespace is accepted. The raw parent is limited to 512 UTF-8 bytes. Future versions must have the known 55-character prefix and a dash before any opaque extension; output uses version `00`. The sampled and random-ID bits survive; other flag bits are cleared. An unsampled parent remains a valid context.

State is validated independently: invalid state is omitted while a valid parent survives. State accepts at most 32 field occurrences, 32 comma-separated members including empty members, and 32,768 combined ASCII input bytes including separators. Duplicate keys invalidate the complete state. Keys and values follow the published grammar; empty members and surrounding HTTP whitespace are removed, while leading spaces inside a non-empty value are preserved. Valid entries keep their input order.

Canonical outgoing state is limited to 512 bytes. When truncation is needed, entries longer than 128 bytes are removed from the right first, followed by remaining entries from the right. Entries are removed whole. There is no baggage, arbitrary-header capture or input-payload logging.

Wire state is retained independently of SDK validators: the pinned Rust SDK rejects a valid maximum-length tenant/system key that Python accepts. A wire-to-SDK conversion must not silently discard that state. `from_sdk` supports the opposite direction, copying a valid SDK identity and normalising its outgoing state. Rust contexts have private fields; Python contexts enforce valid identity, known flags and canonical state at construction.

## Checks and remaining integration

Run the shared vectors and SDK-boundary checks from the repository root:

```sh
cargo test --locked -p quux-otelc-export carrier
.venv/bin/python -m unittest discover -s tests -p test_python_carrier.py -v
make check
```

The full gate enforces independent minimum 80% line coverage for each carrier implementation. Compiler coverage provenance includes the shared JSON fixture, so changing a vector invalidates an older coverage manifest.

Runtime integrations still need independent local-root ownership, inbound/outbound span creation, child IDs, sampling continuity, application-header precedence, telemetry suppression, concurrent request isolation and cleanup after exceptions/cancellation. They must retain canonical wire state across SDK boundaries. The qualified Python stdlib integration has two-process stored-trace evidence; mixed-language traces remain outstanding. Native bindings and JavaScript, TypeScript, Java and Go carrier implementations are separate work under the [context and lifetime plan](context-and-lifetime-plan.md).
