# Prepared Python source origins

<!-- markdownlint-disable MD013 -->

Tooling can explicitly select `python-native-origins-v2` through `otelc.prepared_python.protocols`, `prepare`, `command` and `run`. The same common policy still controls sources, functions, lifetimes, context, traces and live metrics. The application needs no instrumentation imports, annotations or startup changes. The default [input profile](python-prepared-launch.md) keeps its existing formats and behaviour.

This profile uses the [native entry](python-native-entry.md), [source-loader](python-loader-origins.md) and [SDK code-join](python-origin-sdk-joins.md) components. Its declared image boundary is shared/framework GIL CPython 3.12 or 3.14 on macOS ARM64 with a supported inspectable native image. Preparation refuses an unavailable origin image before starting an application or SDK. Module entries need separate support; this profile currently accepts a source `.py` entry inside the application root.

## Run the unchanged shared example

Start the documented [local collector stack](benchmarks.md), then run from the checkout with the prepared Python environment:

```sh
printf '3\nquit\n' | .venv/bin/python -c '
from pathlib import Path
from tempfile import mkdtemp
from otelc.config import Policy
from otelc import prepared_python

runtime = Path(mkdtemp(prefix="otelc-origins-")) / "runtime"
print(f"Runtime: {runtime}", flush=True)
raise SystemExit(prepared_python.run(
    Policy.load(Path("examples/otelc.toml")),
    runtime,
    root=Path.cwd(),
    entry="examples/python/BenchmarkApp.py",
    profile="python-native-origins-v2",
))
'
```

This creates a new runtime directory, runs three workload calls and exits. For interactive input, run the same command without the `printf` pipe; enter `3`, then `quit`. The source remains the ordinary example:

```python
def workload(seed):
    if seed < 0:
        raise ValueError('negative seed')
    item = WorkItem(seed)
    try:
        value = item.seed
        total = 0
        for _ in range(128):
            value = (value * 1664525 + 1013904223) & 65535
            total += value
        return total
    finally:
        item.close()
```

There is no annotated or instrumented source variant. The native observer retains actual input/compiler/code relations and the existing external bootstrap installs the SDK monitor. For tooling-owned execution, `prepare(..., profile='python-native-origins-v2')` returns the fixed supervisor command without starting an application or SDK. Its caller must own the returned process group and cleanup, as described in the default prepared guide. `run` owns that lifecycle itself.

Metric collection and emission still change live through `runtime/control.json`; traces remain independent. The same instrumented PID and SDK code ID survive all four metric states. `plain=True` uses the same native origin observer without an SDK or telemetry. That diagnostic plain lane still has capture overhead; use the benchmark driver's direct stock lane for a stock performance baseline.

## Formats and retained evidence

Explicit `protocols(profile='python-native-origins-v2')` returns handshake schema 2 and the profile name. Request, resolved plan, prepared manifest, command and execution formats are version 2. The journal is version 2 with record kinds 1 through 7; code descriptors remain version 2, health remains version 2 and the SDK protocol remains version 1. The separate post-run comparison API currently accepts journal 1, so this profile advertises `code_comparisons=null`. Consumers must select formats explicitly; they must not infer a profile from journal bytes or a producer command.

The request retains the caller's complete policy, original path/argv intent, interpreter and declared environment. The resolved application root drives `--origin-loader-root` in the independently derived native command. The manifest binds the origin-enabled native driver, its build record and maintained producer files. `python-application-start.json` retains that actual native command and application/SDK ownership. The source journal is used in both lanes.

`python-execution.json` retains the existing entry/input/code summaries plus `entry_evidence.origins`: journal schema, configured root, root IDs, first-execution IDs, SDK code joins and explicit failure labels. The supervisor checks the consumed entry against the separately prepared file identity. Every reported instrumented SDK code must have an origin join; a plain lane must have no SDK code records or joins. Raw loss, wrong scope, missing entry roots and unbound SDK codes cannot become complete entry evidence. A consumer must still decode the raw journal and bind selected code against its own frozen expectations; a summary is not independent admission.

SDK code IDs identify source code objects. They are not runtime trace/span IDs or per-invocation attribution. They appear as attributes on function spans and metrics. Repeated calls reuse a join; separate source compilations keep separate identities.

## Current limits and checks

Every preparation and final execution still reports `source_complete=false`. A complete entry-evidence phase describes this declared capture scope, not whole-program/dependency closure or downstream delivery. This source-only profile does not support cached application bytecode. The separate explicit [cache-origin candidate](python-cached-origins.md) adds ordinary source-loader caches with journal 3; it does not silently change this profile. Custom loaders, arbitrary dynamic compilation, forks/subinterpreters, module entries and other images remain unsupported or unqualified. Cached standard libraries outside the configured application root do not invalidate capture. An unsupported selected source keeps original application output/exit in the receipt but prevents a successful tooling result; failed applications retain their original failure code.

The focused tests run untouched entry/import/standard-library apps in both lanes, preserve output/source and check actual decoded OTLP code IDs. They also cover all four live metric states in one PID, cached-source refusal, independent entry/root/role mismatches, unavailable images and unchanged default protocol behaviour. Data-only summary tests do not claim native producer authenticity. The summary has a separate 80% line floor in addition to existing native and overall gates.

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_python_prepared_origins test_python_origin_execution \
  test_python_prepared test_python_recipe test_python_provider_protocol
make check
```

Independent review, required hosted checks, both-lane consumer qualification and protected delivery remain necessary before this profile can support a broader Python admission claim.
