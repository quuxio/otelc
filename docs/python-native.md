# Native Python frontend candidate

<!-- markdownlint-disable MD013 -->

This candidate adds selected function metrics/spans and live metric controls to original CPython sources. It uses the official SDK tracer, context, sampling and metric/OTLP pipeline. The public `plan` still reports Python as remaining: lifetime observation, official library instrumentors, complete source evidence, benchmarks, independent review and oteleq qualification are delivery work.

Script entries are validated source files inside the selected root. A relative filename beginning with `-` is passed with a `./` prefix so CPython treats it as a file. Application arguments, including option-shaped strings, remain literal arguments after the entry. Other entry spelling and module-name validation are unchanged.

## An unchanged application

[BenchmarkApp.py](../examples/python/BenchmarkApp.py) is an ordinary application with no telemetry imports, decorators, annotations or bootstrap. Its workload uses the same seeded recurrence as the Java, JavaScript and TypeScript examples:

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

Use this function-only common policy for the current candidate. It contains no Python-specific settings and can drive the other language versions too. The full example policy requests lifetimes, which this candidate currently refuses explicitly.

```toml
schema_version = 3

[resource]
service_name = "otelc-examples"

[sources]
include = ["examples/**"]

[functions]
include = ["*workload*"]
exclude = ["*excluded*"]

[export]
endpoint = "http://127.0.0.1:4320"

[metrics]
collection = false
emission = false

[traces]
enabled = true
sample_ratio = 1.0

[lifetimes]
enabled = false
```

Save it as `/tmp/otelc-functions.toml`. From a prepared checkout, start the [local collection stack](../README.md#java-example-and-grafana), then launch the original source:

```sh
make stack-up
runtime="$(mktemp -d /tmp/otelc-python.XXXXXX)/runtime"
echo "$runtime"
.venv/bin/python -m otelc --config /tmp/otelc-functions.toml python \
  --root "$PWD" --entry examples/python/BenchmarkApp.py --runtime "$runtime"
```

Enter `10` for a batch of ten calls. Enter `throw` for a caught exception, or `quit` to finish. App stdout remains app output. The SDK drains in its own process after app execution. An unsuccessful drain returns failure when the app succeeded; an app failure keeps its original exit code.

In another terminal, use the printed runtime path from the first terminal:

```sh
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json status
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json collection on
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json emission on
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json flush
python -m otelc metrics --socket /tmp/otelc-python.XXXXXX/runtime/control.json off
```

Use `.venv/bin/python` if `python` is not available. The endpoint is private authenticated loopback; the shared client reads its token without printing it. Collection and emission switch in the existing app/SDK pair, including from initially off. Disabling metrics leaves traces enabled. An admitted export remains pending until its actual outcome; disabling emission does not cancel it. New controls do not hold the application's observation lock while waiting for an SDK acknowledgement.

Module entry is also native: replace `--entry ...` with `--module package.module`. Place app arguments after `--`. `--plain` starts the same native source without instrumentation or SDK runtime files.

## What is observed

Sources use the common root-relative glob selectors. Function identity is the dotted relative filename, without `.py`, followed by the code object's qualified name. Exclusions take precedence. The tool does not replace functions, code objects, signatures, arguments, return values or exceptions. Native `sys.monitoring` distinguishes completion, unwinding, suspension and resume. Suspended/censored work is never reported as a finished span.

The app-side official tracer has a public OS-entropy ID generator and a primitive span processor. It does not run the OTLP exporter or metric aggregator. The [owned SDK process](python-sdk-process.md) handles aggregation, batching, retries and export without consuming app pseudo-random state. Tracing uses parent-based ratio sampling. Native asyncio task contexts preserve selected-call parentage; that is distinct from qualifying official asyncio/thread/HTTP library instrumentors.

Selected calls produce `otelc.function.calls`, `otelc.function.duration` and `otelc.function.exceptions`. Duration uses a monotonic clock and stops before completion-side telemetry work. A call must enter and complete with collection enabled to contribute function metrics. An off/on interval inside a call can remain part of its elapsed time; these counters do not claim continuous recording during that gap. Metric records carry the existing span context to the official SDK exemplar pipeline. Export state and source completion counts are separate from downstream receipt.

Exception probes read only the raw exception type name; they do not invoke application `str`, `repr` or exception message getters. Failure recovery abandons the probe's context without inventing a completed span and retains the loss in final evidence. SDK span, event and link attribute-drop counts survive the process bridge.

The generated startup hook restores original import paths/PYTHONPATH, chains the original `sitecustomize` through CPython, and removes only its temporary module when no original exists. Startup failure exits before application code rather than allowing CPython to swallow a failed instrumentation hook. An incomplete control client has a total read deadline and cannot hold up app shutdown.

## Evidence and current limits

Local integration tests use CPython 3.12 and the locked SDK. They compare original script/module metadata, arguments, source hashes, seeded random values, signatures, output, tracebacks and exit codes against native baselines. Real receivers decode the four-state example's metric counts and spans, bound to the actual application PID rather than the CLI/SDK PID. Separate tests cover recursion/context boundaries, async suspension/cancellation, occupied monitoring IDs, controlled startup/probe failures, queue/transport evidence and open control connections. These are tested profiles, not a claim about arbitrary applications.

The launcher currently uses otelc's interpreter/environment; install app dependencies there. Arbitrary interpreter/virtualenv selection, isolated/no-site modes, interactive/zip/bytecode entry, fork/process observation, arbitrary context manipulation, annotation reading, complete source provenance and common `health` presentation are not yet qualified. Enabled lifetime or library-context policy currently causes a pre-launch refusal. Benchmark/Grafana comparisons for Python remain outstanding; function telemetry can already reach the configured Collector, but no Python performance result is claimed by these tests.

Run the focused tests from a prepared checkout:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest \
  test_python_monitor test_python_native test_python_frontend_components -v
make check
```

CPython suppresses trace events while running monitoring callbacks. Coverage therefore combines real VM integration with direct internal component tests; the latter are test fixtures, not required developer code changes. Required main-target checks, independent review and immutable-provider oteleq acceptance still apply before Python can be promoted to implemented.
