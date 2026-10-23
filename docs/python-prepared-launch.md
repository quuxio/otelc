# Prepared native Python launch

<!-- markdownlint-disable MD013 -->

The Python candidate's public launcher prepares a fixed native recipe, then owns one application and its independent SDK through completion. Developer sources need no imports, decorators, annotations or bootstrap changes. [Function, lifetime, context and live metric controls](python-native.md) retain the same common policy.

## Run or prepare

Use a new runtime directory for each attempt:

```sh
runtime="$(mktemp -d /tmp/otelc-python.XXXXXX)/runtime"
.venv/bin/python -m otelc --config examples/otelc.toml python \
  --root "$PWD" --entry examples/python/BenchmarkApp.py --runtime "$runtime"
```

Add `--prepare-only` to create the request, resolved plan and verified tool manifest and print their fixed command as JSON. This prepares the native tool without starting an app or SDK. String script paths retain their spelling, including `./`; script paths resolve from `--root`. Module entries use `--module package.module`. App arguments follow `--`.

Tooling callers can use `otelc.prepared_python.prepare`, `command` and `run`. `invocation(runtime)` derives the fixed supervisor command for an existing preparation. The supervisor rechecks that preparation before any SDK/app work. `run` also owns a separate session and cleans it up if the supervisor is interrupted or killed. A consumer using only `command` or `invocation` must retain equivalent process-group ownership through completion.

The supervisor itself uses isolated/no-site startup, so it cannot run installed application startup hooks a second time. The actual native app retains its ordinary startup hooks and default interpreter flags.

The native component requires the matching shared/framework GIL CPython 3.12+ toolchain and Clang described in [native entry inputs](python-input-journal.md). Normal and isolated interpreter startups can spell a Homebrew header directory differently; the build identity binds its canonical header tree, while retaining the actual compiler configuration and flags.

`--plain` uses the same native entry journal without probes, generated instrumentation startup or an SDK. It still retains the preparation and actual execution receipt. Existing output directories/files are refused rather than replaced.

## Retained evidence

The private runtime retains:

- `python-request.json`, `python-resolved-plan.json` and `python-prepared.json`: original common policy/TOML/caller inputs, separately derived settings, native driver/build identity, maintained producer files and the stable selected entry.
- `python-supervisor-start.json` and `python-application-start.json`: actual parent/supervisor/application/SDK identities, process group and SDK readiness before app work.
- `python-inputs.journal`: bounded bytes actually consumed for an ordinary script entry, with actual PID and opened-file metadata. Module import evidence remains unavailable.
- `sdk/python-sdk-spawned.json`, readiness/final status, private SDK diagnostics and any startup-failure receipt: actual worker ownership and exit, including a failed start which never reaches app code.
- `python-execution.json`: actual app/SDK exits, entry evidence, post-drain request/tool/input/bootstrap rechecks and explicit failures. `python-supervisor-error.json` contains owner diagnostics; these are not added to application stdout/stderr.

Normal exceptions and app failures preserve native output and exit codes. The receipt separately retains a signal's negative application return code; the supervisor uses the shell-style `128+signal` result. An otherwise successful app returns failure when required ownership, drain, entry binding or stable-input rechecks fail. An unsuccessful app retains its failure code. Abrupt `_exit`, worker death, missing final records and changed inputs cannot become a successful receipt.

`phase=closed` describes this owned execution and SDK drain, not full source provenance or collector delivery. Every prepared/final response still has `source_complete=false`. A module may complete its lifecycle while its entry/import evidence remains explicitly unavailable. `null` identities/exits identify unavailable evidence; they are not healthy zero counters.

## Current boundary

This profile requires a stable selected entry and unchanged policy/tooling through final drain. A program can still execute and rewrite its own source; the consumed entry bytes remain in the journal, while the stable-input recheck fails. Source snapshots can change concurrent file-mutation timing. Full import/pyc/header/code binding, actual monitored-code identity, native/library and installed-dependency closure, common `health` presentation and frozen-provider oteleq admission remain outstanding. Forks, subinterpreters, custom loaders and arbitrary interpreter/flag profiles remain unqualified.

The existing [benchmark driver](benchmarks.md) retains its lower-level native launch/bootstrap measurement profile. Its application/SDK timing windows do not yet carry these prepared-supervisor receipts. Qualifying that integration is separate from the public launch tests.
