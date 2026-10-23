# Java private-handle action bindings

<!-- markdownlint-disable MD013 -->

The `java-sync-private-handle-v1` candidate combines offline bindings with actual operand verification for ordinary generated callers. It has a separate explicit optional launch/control profile. Generated consumer integration, independent qualification and shipping admission remain outstanding. Default Java launch and `java-sync-action-v2` continue to refuse this binding format; this candidate does not mark another language or whole-provider capability complete.

## Offline compiler

`io.quux.otelc.witness.HandleBindings.compile(classes, declarations)` takes the same original class-byte map and four exact envelope/rendezvous/wrapper/target declarations as `WitnessBindings.compile`. It returns an immutable role-binding map and a route. The standalone `HandleBindings` command takes the same sixteen ordered classfile/owner/name/descriptor arguments as `WitnessBindings`; use the extension JAR and its `-helper-tool.jar` on the classpath. Neither compiler loads or initialises application classes. The existing compiler continues to reject reflection and MethodHandle wrappers.

The wrapper is static and unsynchronised. Its complete executable body is its own static-final, nonvolatile `MethodHandle` field read, exact ordered parameter loads, one `MethodHandle.invokeExact` and the matching return. No argument adaptation, binding, observers, catch blocks, branches, extra field reads or reflection are allowed in that body. Handle construction, constructor/factory calls, argument setup, state observations and error handling belong outside the envelope and use the same ordinary JDK code in both lanes. Original application and dependency bytes remain unchanged.

Targets are ordinary fixed-arity executable methods on concrete classes, including private static/instance and public/final instance methods. The invocation descriptor is the target descriptor, with the exact declaring-class receiver prepended for instance methods. Long/double local slots and the JVM invocation slot bound are checked. The offline result binds code shape; it cannot establish the field's actual handle or prove that an invocation reached its target.

The closed UTF-8 JSON result has exactly `schema_version: 3`, `kind: otelc.java-witness-bindings`, `profile: java-sync-private-handle-v1`, `bindings` and `route`. Each role keeps the existing six owner/name/descriptor/class-hash/method-hash/probe-ID fields. Its probe ID uses the new profile in the existing newline-delimited digest. No v2 probe can substitute for it.

`route` has exactly these fields:

| Field | Value |
| --- | --- |
| `kind` | `direct_method_handle` |
| `field_owner`, `field_name` | Exact original wrapper owner and declared field name |
| `field_descriptor` | `Ljava/lang/invoke/MethodHandle;` |
| `site_instruction` | Integer `0`, counting executable instructions only |
| `invoke_owner`, `invoke_name` | `java/lang/invoke/MethodHandle`, `invokeExact` |
| `invoke_descriptor` | Exact wrapper/handle invocation descriptor |
| `reference_kind` | Integer `6` for static or `5` for virtual, including private instance methods |
| `sha256` | Lowercase route digest described below |

The route SHA-256 covers UTF-8 newline-joined values in this exact order, without a trailing newline: profile, kind, field owner, field name, field descriptor, decimal site instruction, invoke owner, invoke name, invoke descriptor, decimal reference kind, wrapper probe ID, target probe ID. Every original field declaration and initialiser byte is also covered by the role's original class hash.

## Runtime candidate

The route uses explicit version `3` for its generated plan, optional launch descriptor, socket exchanges and final report. The v2 profile and refusal rules remain exact, without version inference or fallback. The ordinary prepared-launch identity and default runtime-health formats stay unchanged.

The launch descriptor uses the existing `otelc.java-witness-launch` kind and fields with the new profile/version, plus `route` above. Tooling identity binds the original bindings and generated plan/descriptor. The generated plan adds the route members under `route.` keys. Runtime replies add `route_binding_sha256` to the v2 header fields; scopes add boolean `route_verified`, initially false. The final `otelc.java-witness-final` document retains that version/header/scope structure. Requests begin with `3`; `arm` appends the expected route digest after the independently expected thread ID. Other word positions and identity checks stay the same as v2.

Observe the actual operand produced by the bound `GETSTATIC` after that instruction completes. A fresh proof must belong to the current wrapper entry and exact PID/thread/cookie/sequence/scope, never an earlier entry or class initialisation. Do not re-read the field or invoke an application getter. Reveal the actual direct handle using an ordinary JDK lookup for the exact loaded target class. Require its declaring Class, name, MethodType, reference kind, exact invocation type and fixed arity to match the frozen route. Reject adapted/bound handles, varargs collectors, mutable callsites, reflection fallback and named-module access that would require new opens.

The initial instance profile requires the actual target receiver's exact Class. Subclasses, interface dispatch and inherited/overridden dispatch are unsupported. An actual target entry must follow only the verified JDK handle machinery back to that wrapper. Intervening application/initialiser frames are unrelated work and cannot consume proof. Retire proof on every exit and refusal. Producer completion additionally requires verified route evidence. Independent decoded receipt, complete default SDK health, original output/state/error observations and existing scope/loss checks remain mandatory.

Real-agent tests cover private static/instance and public/final instance targets, original output/state/error identity, all live metric states, wrong direct handles with the same output, adapted/bound handles restored to the same type, target initialiser noise, helper collisions, exact receiver refusal and direct recursion. They independently decode span IDs, parentage, nine typed identity attributes and zero reported attribute/event/link drops. Wrong thread, route digest and stale runtime arms do not consume readiness. Cancellation and delayed/failed export remain explicit incomplete outcomes. These tests do not establish generated consumer or shipping admission.

## Use the explicit diagnostic API

```python
from otelc import prepared_java, witness

profile = witness.HANDLE_PROFILE
bindings = witness.compile_bindings(application, declarations, bindings_path, profile=profile)
launch = prepared_java.prepare(application, policy, runtime,
    witness_bindings=bindings_path, witness_profile=profile)
# Start launch.argv through the owning supervisor and retain its actual PID.
# Use the independent SDK-free caller channel to obtain its logical thread ID.
status = witness.request(runtime / "witness.sock", "status", profile=profile)
# Arm using the frozen launch IDs, cookie/sequence, independently expected
# thread ID and bindings["route"]["sha256"], then release the ordinary caller.
# After actual scope end and process exit, retain the closed final report.
final = witness.read_final(runtime / "witness-health.json", profile=profile)
```

`runtime` is the new owned private directory required by the prepared API. Keep the ordinary plain caller identical and omit witness options in that lane. The API's default profile stays v2; a schema-3 document or reply cannot select the new profile implicitly. Choosing a nondefault profile without bindings is refused before normal launch preparation. The CLI requires both explicit options:

```sh
bin/quux-otelc java --manifest application.json --runtime /tmp/new-handle-runtime \
  --witness-bindings bindings.json --witness-profile java-sync-private-handle-v1
```

This diagnostic command still needs an ordinary caller rendezvous and an owning supervisor; it does not create a generated test suite automatically.
