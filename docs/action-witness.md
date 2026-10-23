# Invocation attribution: Java producer work

<!-- markdownlint-disable MD013 -->

The existing prepared Java API proves source/build bindings and exposes SDK health. It does **not** attribute an arbitrary concurrent call to a specific test action. Name/tree-based consumer qualification keeps that limit. This work implements the producer side of the [oteleq proposal](https://github.com/quuxio/oteleq/blob/main/docs/action-witness.md) in stages; it does not replace the full multi-language completion criteria.

## Current implementation

`WitnessBindings` is an offline compiler for four exact method roles: envelope, rendezvous, wrapper and target. It reads class bytes without loading or executing application classes. Each role contains the exact JVM owner/name/descriptor, original class SHA-256, canonical method SHA-256 and a role-specific probe ID. Distinct overloads remain distinct; changing any original bytes changes the affected class's probe IDs. Canonical method bytes omit debug tables and frames, but the original class digest still binds those bytes.

The compiler accepts static, unsynchronised ordinary control methods and ordinary static or receiver-based target calls, including Unicode Java identifiers. The rendezvous is `()V`. The wrapper loads arguments, invokes exactly the declared target, then returns the result directly; it cannot catch exceptions or run observers. The envelope calls the rendezvous once, loads the already-prepared wrapper arguments, invokes the wrapper once and returns its result. No callback, branch, switch or exception handler may bypass or intervene in that tail. Decoding, fixture construction and observations belong outside the wrapper; the surrounding harness catches errors after envelope exit.

This is structural binding evidence. It does **not** prove that a class loaded, transformation succeeded, an action ran, virtual dispatch reached the selected implementation, telemetry arrived, or an invocation completed. JVM verification and real execution remain necessary. It is not a hostile-bytecode verifier or a semantic equivalence proof.

`WitnessMethodGuard` checks the actual incoming method stream of a ByteBuddy transformation against the canonical method bindings. It buffers each declared method before passing it to downstream advice and reports matches only after every expected method has matched. Changed, missing, duplicated or omitted methods, a wrong class owner and incorrect advice/guard ordering fail. Canonical hashing discards debug tables/frames on a copy; the executable output keeps them. Tests transform and load real bytecode, run the resulting method and check exceptional exits. The optional runtime profile connects the guard to actual loaded origins, role-probe execution, live context and OTLP attribution. A method-stream match alone does not attest the complete original class bytes or prove JVM definition.

`WitnessClassCapture` is the read-only input-capture building block for that runtime. It registers before the pinned agent's main transformer, checks full original class bytes and canonical selected methods against the bindings, and pairs them with the actual system-loader `Class` identity and exact code-source URL after definition. It does not initialise the application classes. Only the four declared role owners are retained. Changed origins/bytes, a preloaded owner, another classloader, duplicate definitions and redefinition prevent verification. Failures remain recorded because the JVM can ignore exceptions thrown by a transformer. Retiring the capture prevents further verification.

Real pinned-agent tests cover directory and ordinary JAR origins, selected and excluded methods, an upstream byte change, changed origins, preloaded owners and retransformation. A second read-only transformer after the SDK verifies that capture sees the original selected method before the SDK adds its advice; both instrumented and plain applications return the same value, and class initialisation still happens at ordinary application execution. macOS callers must resolve symlinks before deriving an expected code-source URL: `/var` and `/private/var` can name the same file but are different literal origins.

Capture is installed before the pinned agent transformer when `--witness-bindings` explicitly requests the profile. It supplies original-input evidence; installed guards and actual role execution supply separate checks. Loading bound owners does not initialise them. Redefinition invalidates the capture, and mutation commands and final evidence recheck it. Default launches keep capture disabled and retain their existing launch/health schemas.

Run the ordinary fixture and generate its bindings:

```sh
make java-witness-bindings
printf '\n' | java -cp build/witness/java/classes examples.WitnessHarness 0
cat build/witness/java/bindings.json
```

The fixture uses the existing unedited `BenchmarkApp.workload`. `WitnessHarness` is an ordinary diagnostic harness shared by both future lanes. Neither contains an SDK import, annotation or telemetry call. Its stdin release is only a fixture channel; the plain command above does not start the producer protocol or emit scoped spans.

The binding document has this closed shape:

```json
{
  "schema_version": 2,
  "kind": "otelc.java-witness-bindings",
  "profile": "java-sync-action-v2",
  "bindings": {
    "envelope": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "envelope", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "rendezvous": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "rendezvous", "descriptor": "()V", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "wrapper": {"probe_id": "SHA256", "owner": "examples/WitnessHarness", "name": "wrapper", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"},
    "target": {"probe_id": "SHA256", "owner": "examples/BenchmarkApp", "name": "workload", "descriptor": "(I)J", "class_sha256": "SHA256", "method_sha256": "SHA256"}
  }
}
```

Probe ID is SHA-256 over UTF-8 fields joined with a single LF and no final LF, in order: profile, role, internal owner, method name, descriptor, original class SHA-256 and canonical method SHA-256. Hashes use lowercase hexadecimal. The runtime independently recompiles the structural bindings from the captured original bytes; a self-consistent edited binding document alone cannot enable an action. A consumer must bind to a frozen, gated source/product snapshot. The method canonicalisation is pinned to the current compiler/ASM implementation; consumers retain frozen tooling identity rather than reconstructing it with a different library.

## Runtime profile

The `java-sync-action-v2` capability supports one active synchronous action on a platform thread and sequential actions in the same JVM. Use the ordinary prepared manifest for both plain and instrumented lanes. Compile bindings outside the agent JVM with `otelc.witness.compile_bindings(application, declarations, output)`; each declaration contains the original owner/name/descriptor. Then request them explicitly:

```sh
bin/quux-otelc java --help
bin/quux-otelc java --manifest application.json --runtime /tmp/otelc-action --witness-bindings bindings.json --diagnostic-profile
```

The runtime directory must be fresh. The common policy must select the target, enable tracing and set `sample_ratio = 1.0`. Plain comparison uses the same manifest and arguments with `--plain`, without `--witness-bindings`. Application source and class files are never edited by this profile. The optional argument supplies diagnostic role bindings, not developer-authored telemetry. See [the wire and evidence reference](java-witness-protocol.md) for the Python API and protocol.

The exchange is ready → arm → acknowledgement → harness release → consumed → root/envelope exit → end/export evidence. An actual instrumented envelope establishes a fresh cookie, sequence and immutable platform-thread ID before arm. Every reply identifies the actual JVM. The consumer requires the PID and thread independently reported by its SDK-free harness before arming; a foreign envelope becoming ready first cannot stand in for the intended action. The wrapper, target and rendezvous must be the declared loaded methods in that envelope; names and thread IDs alone cannot establish ownership. Re-entrant envelopes, reflected unrelated wrappers and concurrent noise do not consume another action's arm. Class-initialiser calls cannot stand in for the target. Receiver dispatch reaching an override instead of the bound implementation leaves target evidence missing.

The wrapper creates an explicit diagnostic trace root; the bound target and ordinary selected function spans are children. Supported same-thread recursion retains the context. Errors keep the original throwable and only record its type, without invoking message/cause/formatting getters. Metrics switches do not suppress tracing. Lifetime roots with links remain separate traces and are not silently counted as action children.

A successful `end` requires root and envelope exits, an observed target, matching started/ended/exported IDs, no pending exports or sticky scope failure, and a successful bounded SDK flush checkpoint. An HTTP success or SDK flush alone cannot establish downstream receipt. Delayed exports can recover through another `end`; the earlier flush failures remain recorded. Failed exports, cancellation, expiry, missing targets and process termination retain incomplete outcomes. A tagged late span invalidates an earlier checkpoint.

`witness-health.json` retains all contexts after the existing bounded SDK shutdown. Queue drops remain unknown until a supported global drain allows per-scope derivation; failed export attempts remain separate. Missing final evidence must be rejected. The producer never claims application-wide quiescence, semantic equivalence, all-call discovery or downstream receipt.

Limits: 64 retained contexts per JVM, 1024 observed spans per context, a 30-second pre-consumption readiness/arm deadline, a 1500 ms `end` flush wait, one active action, system-loader ordinary classes and a Unix-domain socket. Execution after consumption has no invented 30-second completion deadline. Limits fail explicitly; the profile does not silently fall back to tree matching.

Run the real-agent positive and negative controls:

```sh
make check
.venv/bin/python -m unittest discover -s tests -p test_witness.py -v
```

The tests compile bindings in an offline process, use unchanged plain/instrumented applications, decode OTLP independently, exercise static and receiver calls, exceptions, stale runtime identity, cancellation, delayed and failed exports, substituted virtual dispatch, helper collisions and class initialisation. A returned pending future keeps the observed function span open and prevents completion until the original future settles; this does not qualify general async context propagation. Another control switches all four metric states in one JVM while action tracing continues. Unit controls cover recursion, reflection/re-entry/concurrency noise, malformed sockets, wrong identities, expiry, late spans and class redefinition. These controls qualify this synchronous profile, not virtual threads, async migration or arbitrary classloaders.

## Acceptance checklist

- [x] Offline exact overload/class/method bindings and ordinary static/receiver fixtures.
- [x] Reject intervening callbacks, target duplication, caught errors and bypass control flow.
- [x] Verify incoming transformation bytes and installed role probes against original bindings.
- [x] Live envelope identity, readiness, bounded arm/consume/end/cancel exchange and retirement.
- [x] Actual OTLP root/target identities, scope propagation through supported recursion and exceptions.
- [x] Explicit per-scope pending/loss/export/completion evidence, independently checked against received OTLP.
- [x] Stale, cross-runtime, wrong-context, overlapping, duplicate and missing-event negative controls.
- [ ] Existing prepared API compatibility, final review, required CI/Sonar and frozen consumer handoff.
- [ ] Separately qualify virtual threads, async/context migration, detached work, concurrent actions and other languages.

The last item remains part of the project; the first synchronous capability does not close it. Lifetime observation and application-observation equivalence also retain their separate limits.
