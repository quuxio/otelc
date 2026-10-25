# C++ object lifetime probes

<!-- markdownlint-disable MD013 -->

Status: development component. The LLVM 22 object profile connects the [Clang semantic inventory](native-lifetimes.md) to compiler-inserted probes and the official C++ SDK. Internal tests execute unchanged C++ application source with both function and object probes. The public native launcher still refuses `lifetimes.enabled`; public policy/bootstrap/health wiring, downstream OTLP receipt, benchmarks and independent oteleq qualification remain work.

## Observed boundaries

A complete-object observation begins only on the successful return edge of its selected constructor. A constructor that throws creates no observation. The lifetime duration ends at the first entry to the complete destructor, matching C++'s lifetime boundary for a class with a non-trivial destructor. Cleanup completion is tracked separately; `otelc.destructor.exception_exit` records whether an exception leaves that destructor. An object destroyed during stack unwinding is not necessarily a destructor that throws.

The pass uses canonical Clang type identities, constructor/destructor ABI symbols and physical/debug source coordinates. It does not infer types from names alone. Complete/base/delegating variants must not create duplicate observations. The current combined profile is `llvm22-itanium-objects-v1`, with function ABI 2 and object probe ABI `otelc.object.v1`.

The SDK engine keeps bounded metadata without retaining application objects. Addresses and canonical types identify live observations internally. A generation token separates cleanup completion from a later object at the same address. Destruction can occur on another thread. Raw addresses and per-instance tokens are not metric labels.

Lifetime spans are roots with a link to the selected function context captured at successful construction. Explicit system and steady timestamps retain the observed interval; the span is exported after completion rather than holding an SDK span throughout the object's life. Cross-thread destruction does not itself establish task-context propagation. Remaining observations at shutdown produce `shutdown_censored` spans and incomplete counts, never fabricated destruction times.

The instruments are `otelc.lifetime.admitted`, `otelc.lifetime.completed`, `otelc.lifetime.incomplete`, `otelc.lifetime.duration` and `otelc.lifetime.live`. They use the existing independent metric collection/emission controls. Tracing stays independent. Capacity includes observations whose export is still ending, so a blocked exporter cannot bypass the configured live-observation limit.

## Contributor checks

With LLVM 22, CMake and Ninja installed, run:

```sh
make native-check
```

This builds the combined compiler plugin, inspects `tests/fixtures/native-objects.cpp` through Clang, and links the unchanged fixture into the actual SDK test. The fixture contains no telemetry imports or probe calls. Internal host-side test code supplies the ABI bridge; developers will not add that bridge to applications.

The check runs the controls, function and object SDK test groups. It independently requires at least 80% line coverage for each SDK implementation and for `native/llvm/ObjectPass.cpp`. Compiler and application profiles are merged separately. Exact source identities, compiler context, coverage maps and the linked test products are retained under `build/native-*` for the shared Sonar converter. Invalid schema, missing translation units, duplicate type identities, ambiguous or missing constructor sites, trivial construction and an absent inventory must fail compilation.

The maintained tests cover constructor failure, arrays, copy/delegation, address reuse following destructor entry, cross-thread destruction, exception cleanup, throwing destructors, creation-context links, metric switches, bounded admission and incomplete shutdown observations. A separate development execution also compares the combined profile at `-O0` and `-O2`; that evidence does not qualify arbitrary optimisations or build recipes.

## Remaining scope

This component is not general C++ lifetime support. Trivial and aggregate construction are refused by this profile. Materialisation-only boundaries are unsupported and are not evidence of a successful observation. Multi-translation-unit build integration, implicit storage reuse without a destructor, arbitrary compiler flags, Microsoft C++ ABI, resource-close pairing and native task/service propagation remain unsupported. The inventory still declares `source_complete=false`. Public launch/export and performance evidence must pass before advertising the feature as available to application developers.
