# Native object SDK bridge

<!-- markdownlint-disable MD013 -->

Status: development component. The launcher shared library now connects the compiler's object ABI to the reviewed [object engine](native-object-probes.md), the official C++ SDK HTTP exporters, live metric controls and runtime health. The [public build-4 candidate](native-public-lifetimes.md) now connects each application's actual compiler context to a frozen lifetime inventory and owned preparation. The older immediate `native` command still refuses enabled lifetime policies.

Developers add no imports, probes or bootstrap code. The integration test compiles the unchanged maintained C++ fixture with the combined LLVM function/object pass and its actual Clang semantic inventory. A separate generated driver supplies ordinary application calls, and the generated bootstrap attaches the SDK. The internal test host supplies `settings.object_lifetimes = {enabled: true, max_live: ...}` in its inherited plan; this is a component test interface, not another public configuration file.

## Pipeline and shutdown

Successful constructor probes admit observations with the active selected function context. Destructor entry ends the lifetime interval; destructor exit separately records whether an exception escaped. Destruction on another thread does not require a matching thread-local function frame. The engine keeps metadata, not strong application object references.

Shutdown first stops control requests and waits for function frames. It then closes the object engine before flushing and shutting down SDK exporters. Objects that remain live become `shutdown_censored` observations, with incomplete counters. They are never reported as destroyed. If a drain cannot finish within its deadline, health continues to report that boundary; provider storage remains alive for the draining thread.

An enabled bridge adds `object_accounting` to native runtime health: `types`, `admitted`, `completed`, `live`, `destroying`, `ending`, `censored`, `lost`, `duplicates` and `invalid` are decimal counters; `closing` and `closed` are booleans. The reader checks the complete field set and permits it only for C++. Existing function-only launches retain their previous health fields.

Lifetime metrics use the same independent collection/emission gates as function metrics. Collected observations can be retained while emission is off and exported after it is enabled. Tracing remains independent, including when both metric switches are off. Setting the internal bridge's live limit bounds accepted observations; refused capacity is visible as loss rather than a complete lifetime claim.

## Verification and remaining work

`make native-launch-check` includes the object bridge tests using its actual SDK build. They compare plain and instrumented application results, original exceptions, cross-thread destruction, address reuse, creation-context links, shutdown censoring, all four live metric states, bounded admission and tracing disabled. They decode actual OTLP HTTP requests and read final SDK health. These tests do not establish complete provenance, a shipping application benchmark, Grafana delivery or whole C++ language qualification.

Public integration generates an inventory with each application's real flags, working directory and private overlays. A default C++17 inventory cannot certify another compile context. Both lanes must retain original source/input bindings, ownership before constructors and the existing live controls. Native resource-close pairing, C resource observations, broader object materialisation and task/service context remain separate requirements.
