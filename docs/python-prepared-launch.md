# Prepared native Python launch

<!-- markdownlint-disable MD013 -->

The Python candidate's public launcher prepares a fixed native recipe, then owns one application and its independent SDK through completion. Developer sources need no imports, decorators, annotations or bootstrap changes. [Function, lifetime, context and live metric controls](python-native.md) retain the same common policy.

## Run or prepare

Use a new runtime directory for each attempt:

```sh
runtime="$(mktemp -d /tmp/otelc-python.XXXXXX)/runtime"
bin/quux-otelc --config examples/otelc.toml python \
  --root "$PWD" --entry examples/python/BenchmarkApp.py --runtime "$runtime"
```

Add `--prepare-only` to create the request, resolved plan and verified tool manifest and print their fixed command as JSON. This prepares the native tool without starting an app or SDK. String script paths retain their spelling, including `./`; script paths resolve from `--root`. Module entries use `--module package.module`. App arguments follow `--`.

Tooling callers can use `otelc.prepared_python.prepare`, `command` and `run`. `invocation(runtime)` derives the fixed supervisor command for an existing preparation. The supervisor rechecks that preparation before any SDK/app work. `run` also owns a separate session and cleans it up if the supervisor is interrupted or killed. A consumer using only `command` or `invocation` must retain equivalent process-group ownership through completion. On macOS, a denied group signal is treated as an absent group only after a bounded OS process-table query confirms no members remain. A live group, unavailable membership or a failed query still causes cleanup to fail.

The public `bin/quux-otelc` host, fixed supervisor and SDK worker use isolated/no-site startup. The CLI and SDK worker add only explicit installed wheel directories; they do not process `.pth` files, `sitecustomize`, user sites or application `PYTHONPATH`. This keeps command JSON and application byte channels free from tooling-host startup output. The actual native app retains its ordinary startup hooks and default interpreter flags. Editable SDK installs or SDK dependencies made available only through `.pth` files are outside this profile; install the locked wheels with `make setup`.

The wrapper defaults to this checkout's `.venv/bin/python`. To select another SDK environment, set `OTELC_PYTHON=/absolute/path/to/venv/bin/python` before the command. The previous `python -m otelc` CLI is refused because Python has already run site hooks before the module is entered. Tooling callers using the Python API must themselves start in a controlled interpreter when they require clean command/application output.

The native component requires the matching shared/framework GIL CPython 3.12+ toolchain and Clang described in [native entry inputs](python-input-journal.md). Normal and isolated interpreter startups can spell a Homebrew header directory differently; the build identity binds its canonical header tree, while retaining the actual compiler configuration and flags.

`--plain` uses the same native entry and ordinary-loader journals without probes, generated instrumentation startup or an SDK. It still retains the preparation and actual execution receipt. Existing output directories/files are refused rather than replaced.

## Retained evidence

The private runtime retains:

- `python-request.json`, `python-resolved-plan.json` and `python-prepared.json`: original common policy/TOML/caller inputs, separately derived settings, native driver/build identity, maintained producer files and the stable selected entry.
- `python-supervisor-start.json` and `python-application-start.json`: actual parent/supervisor/application/SDK identities, process group and SDK readiness before app work.
- `python-inputs.journal`: bounded bytes actually consumed for an ordinary script entry, with actual PID and opened-file metadata, plus separate whole ordinary-loader source/bytecode reads. Separate selected-code records bind genuine monitored starts and feed function span/metric code IDs; their bounded data format and recorded filename codec are checked after final drain; reads alone do not prove code execution.
- `sdk/python-sdk-spawned.json`, readiness/final status, private SDK diagnostics and any startup-failure receipt: actual worker ownership and exit, including a failed start which never reaches app code.
- `python-execution.json`: actual app/SDK exits, entry evidence, post-drain request/tool/input/bootstrap rechecks and explicit failures. `python-supervisor-error.json` contains owner diagnostics; these are not added to application stdout/stderr.

Normal exceptions and app failures preserve native output and exit codes. The receipt separately retains a signal's negative application return code; the supervisor uses the shell-style `128+signal` result. An otherwise successful app returns failure when required ownership, drain, entry binding or stable-input rechecks fail. An unsuccessful app retains its failure code. Abrupt `_exit`, worker death, missing final records and changed inputs cannot become a successful receipt.

Ordinary monitoring errors preserve application results. If an application audit hook refuses frame access, the probes respect that refusal and record `monitor_frame` loss. They do not retry with another introspection mechanism. Returns, original exception objects and generator/coroutine results remain native; missing observations make the final SDK evidence fail. A call whose completion cannot be observed remains incomplete rather than acquiring a fabricated completion.

Generated metadata becomes readable after the publisher removes its staging hard link. A two-link file raises `python_launch_files.PublicationPending` (a `FileNotFoundError` subclass) without reading any bytes; startup polling retries. A final single-link file then uses the strict owned-file snapshot, including inode, size, permissions, ownership, modification/change times and link-count stability. Other link counts are refused. A fresh read after publication does not authenticate earlier in-progress bytes or their writer; consumers still require the expected request, ownership and final-field checks.

`phase=closed` describes this owned execution and SDK drain, not full source provenance or collector delivery. Every prepared/final response still has `source_complete=false`. A module may complete its lifecycle while its complete entry/code provenance remains explicitly unavailable. `null` identities/exits identify unavailable evidence; they are not healthy zero counters.

## Current boundary

Instrumented launches also publish a [common CLI health checkpoint](python-health.md) at `health.json`. Use `health --socket runtime/control.json` while running or `health --report runtime/health.json` after completion. Health preserves actual process/exits, SDK failures and unavailable observations; it does not establish source closure or Collector receipt. Plain launches have no such telemetry report.

This profile requires a stable selected entry and unchanged policy/tooling through final drain. A program can still execute and rewrite its own source; the consumed entry bytes remain in the journal, while the stable-input recheck fails. Source snapshots can change concurrent file-mutation timing. Ordinary source/whole-pyc reads are retained; source/pyc-to-code attribution, whole-program code coverage, native/library and installed-dependency closure, common `health` presentation and frozen-provider oteleq admission remain outstanding. Forks, subinterpreters, custom loaders and arbitrary interpreter/flag profiles remain unqualified.

The [benchmark driver](benchmarks.md) uses this public recipe in both lanes, binds the retained preparation/execution/journal files and validates actual app/SDK completion before publication. Application latency and application/SDK/supervisor resource windows remain separate. The plain lane still captures native entry and ordinary-loader inputs; it has no telemetry probes or SDK. These receipts do not complete full import/code provenance or frozen-provider admission.
