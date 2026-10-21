# Invocation attribution: Java producer work

<!-- markdownlint-disable MD013 -->

The existing prepared Java API proves source/build bindings and exposes SDK health. It does **not** attribute an arbitrary concurrent call to a specific test action. Name/tree-based consumer qualification keeps that limit. This work implements the producer side of the [oteleq proposal](https://github.com/quuxio/oteleq/blob/main/docs/action-witness.md) in stages; it does not replace the full multi-language completion criteria.

## Current implementation

`WitnessBindings` is an offline compiler for four exact method roles: envelope, rendezvous, wrapper and target. It reads class bytes without loading or executing application classes. Each role contains the exact JVM owner/name/descriptor, original class SHA-256, canonical method SHA-256 and a role-specific probe ID. Distinct overloads remain distinct; changing any original bytes changes the affected class's probe IDs. Canonical method bytes omit debug tables and frames, but the original class digest still binds those bytes.

The compiler accepts static, unsynchronised ordinary control methods and ordinary static or receiver-based target calls, currently with ASCII Java identifiers. The rendezvous is `()V`. The wrapper loads arguments, invokes exactly the declared target, then returns the result directly; it cannot catch exceptions or run observers. The envelope calls the rendezvous once, loads the already-prepared wrapper arguments, invokes the wrapper once and returns its result. No callback, branch, switch or exception handler may bypass or intervene in that tail. Decoding, fixture construction and observations belong outside the wrapper; the surrounding harness catches errors after envelope exit.

This is structural binding evidence. It does **not** prove that a class loaded, transformation succeeded, an action ran, virtual dispatch reached the selected implementation, telemetry arrived, or an invocation completed. JVM verification and real execution remain necessary. It is not a hostile-bytecode verifier or a semantic equivalence proof.

Run the ordinary fixture and generate its bindings:

```sh
make java-witness-bindings
printf '\n' | java -cp build/witness/java/classes examples.WitnessHarness 0
cat build/witness/java/bindings.json
```

The fixture uses the existing unedited `BenchmarkApp.workload`. `WitnessHarness` is an ordinary diagnostic harness shared by both future lanes. Neither contains an SDK import, annotation or telemetry call. Its stdin release is only a fixture channel; this command does not start the proposed producer protocol or emit scoped spans.

The binding document has this closed shape:

```json
{
  "schema_version": 1,
  "kind": "otelc.java-witness-bindings",
  "profile": "java-sync-action-v1",
  "bindings": {
    "envelope": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "envelope", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "rendezvous": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "rendezvous", "descriptor": "()V", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "wrapper": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "wrapper", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "target": {"probe_id": "SHA256", "owner": "examples/BenchmarkApp", "name": "workload", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"}
  }
}
```

Probe ID is SHA-256 over UTF-8 fields joined with a single LF and no final LF, in order: profile, role, internal owner, method name, descriptor, original class SHA-256 and canonical method SHA-256. Hashes use lowercase hexadecimal. This document is not yet an enabled runtime capability or a consumer handoff. The method canonicalisation is pinned to the current compiler/ASM implementation; consumers retain frozen tooling identity rather than reconstructing it with a different library.

## Runtime contract to implement next

The initial runtime capability is synchronous single-action execution on one platform Java thread. An ordinary per-action envelope establishes a fresh live invocation identity. Rendezvous, wrapper and target entries must be bound to their actual transformed methods and that live envelope. A thread ID, process nonce or function name alone is insufficient. Re-entry and unrelated calls cannot consume another invocation's arm.

Producer control will use a separate bounded, versioned protocol. Existing default config-plan output, the 16-field prepared launch metadata and the closed runtime-health schema remain unchanged. A requested witness profile must fail explicitly when unsupported; it must not fall back to names or default launch behaviour.

The implemented wire schema and frozen source will be published before the consumer binds to them. The required exchange remains ready → arm → acknowledgement → harness release → consumed → root exit → export/end evidence, plus cancel and retained incomplete outcomes. Runtime, compiler probes, case/attempt, build/selection, fresh scope and live context must agree throughout. An SDK flush response alone cannot establish downstream receipt.

## Acceptance checklist

- [x] Offline exact overload/class/method bindings and ordinary static/receiver fixtures.
- [x] Reject intervening callbacks, target duplication, caught errors and bypass control flow.
- [ ] Verify incoming transformation bytes and installed role probes against frozen bindings.
- [ ] Live envelope identity, readiness, bounded arm/consume/end/cancel exchange and retirement.
- [ ] Actual OTLP root/target identities, scope propagation through supported recursion and exceptions.
- [ ] Explicit per-scope pending/loss/export/completion evidence, independently checked against received OTLP.
- [ ] Stale, cross-runtime, wrong-context, overlapping, duplicate and missing-event negative controls.
- [ ] Existing prepared API compatibility, final review, required CI/Sonar and frozen consumer handoff.
- [ ] Separately qualify virtual threads, async/context migration, detached work, multiple actions and other languages.

The last item remains part of the project; the first synchronous capability does not close it. Lifetime observation and application-observation equivalence also retain their separate limits.
