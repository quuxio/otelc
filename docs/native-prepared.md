# Native prepared-launch candidate

<!-- markdownlint-disable MD013 -->

This explicit profile separates compilation, preparation and owned execution for unedited C/C++ applications. It uses the common schema-3 policy, LLVM 22, the registered function ABI and the official C++ SDK pipeline. The separate [build-3 context candidate](native-build-context.md) adds explicit per-unit compiler contexts, private helper assets and a neutral observation driver. The [C++ lifetime candidate](native-public-lifetimes.md) adds explicit build 4 through the same preparation/release API. The [C allocation lifetime candidate](c-resources.md) adds explicit build 5 with separate inventory/resolution and resource ABI records. Automatic build discovery, task/service propagation, independent oteleq integration and complete language acceptance remain open.

## Commands

Use the ordinary C++ source and `/tmp/native-demo.toml` policy in the [native launch guide](native-launch.md). Build the SDK once, then retain both application lanes and the fixed execution gate in a new output directory:

```sh
make native-launch-sdk
bin/quux-otelc native-protocols
bin/quux-otelc --config /tmp/native-demo.toml native-build \
  --language cpp --root /tmp/native-demo --source main.cpp \
  --output /tmp/native-prepared-build --sdk-build build/native-launch-sdk
bin/quux-otelc --config /tmp/native-demo.toml native-run \
  --build /tmp/native-prepared-build --runtime /tmp/native-prepared-run
```

`native-protocols` needs neither a policy file nor a compiler. Function-only `native-build --source` writes `native-source-build.json` version 2 and binds the native build, plugin, gate and SDK build records. Preparation and launch recheck the recorded products and inputs without running a compiler. This is an input/product binding; it does not prove complete source origin or installed-library closure.

Add `--plain` to run the compiled uninstrumented lane. Each launch requires a new runtime directory. Use `--` before literal application arguments; the tooling does not interpret them as shell text. Standard input, output and error remain application channels. The SDK control endpoint is `<runtime>/app/control.json`, with the usual `metrics` commands for independent collection and emission switches.

## External hosts and qualification tools

`native-run --prepare-only` writes the preparation records and starts no processes. A host can instead call these Python APIs:

```python
import os
from pathlib import Path
import subprocess

from otelc import prepared_native

runtime = Path('/tmp/native-prepared-run')
supervisor = subprocess.Popen(
    prepared_native.command(runtime), start_new_session=True,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
# Wait for native-application-start.json while checking supervisor.poll().
# Read the private file with the SDK reader and check the child's PID,
# parent, process group and session against actual OS observations.
# Then acknowledge that exact child:
prepared_native.release(runtime, pid=application_pid, supervisor_pid=supervisor.pid)
# Exchange the application's original protocol through its standard streams.
code = supervisor.wait()
result = prepared_native.execution(
    runtime, supervisor_pid=supervisor.pid, parent_pid=os.getpid(), returncode=code,
)
```

The snippet shows the API sequence; the host supplies its bounded readiness loop, OS observations, application protocol and cancellation. The ordinary `native-run` command supplies its own release host. Metadata cannot select a different supervisor script or executable. Preparation records are single-use and bind the selected policy, literal arguments, interpreter and fixed producer files.

The SDK supervisor owns a separate child process group. It publishes readiness while the fixed gate waits, before executing application code or loading application libraries. The host must release that child within its recorded budget. Incorrect acknowledgement, timeout or supervisor interruption cancels the waiting child. Release preserves the child's PID, group, session and streams through `exec`.

## Evidence and limits

`native-execution.json` retains the actual supervisor and waiting-child observations, release state, raw child process exit, supervisor exit, input recheck, SDK checkpoint when available and explicit failures. A non-zero application exit remains non-zero. A killed process cannot gain a successful final SDK checkpoint. Plain execution has no SDK health report.

The public reader validates the closed execution shape, exact integer process identities, retained application record and externally observed supervisor exit. Records without SDK/supervisor failures must join the normalised child exit, including signal termination, to that observed exit. Final health must identify the launched PID, policy, language and function selection from the verified build. The build binds the exact canonical `native-sdk-build.json` record that the reader consumes. `exec_verified=false` is deliberate: acknowledgement alone does not prove which image executed. `source_complete=false` remains explicit. SDK health does not establish downstream Collector receipt; qualify decoded telemetry separately.

Tests run real plain and woven C++ executables, including an application constructor, original exceptions, literal arguments, all four live metric states, non-zero exits, killed applications, interrupted supervisors, wrong release and expired release budgets. Tests also refuse altered source, prepared argv and execution records. The full repository and independent review gates remain required before publication and language admission.
