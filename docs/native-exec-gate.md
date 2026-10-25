# Native pre-exec ownership gate component

The gate starts a fixed tool-owned C helper in a new process session. It reports its PID, parent, process group and session through an inherited pipe, then waits for a separate host acknowledgement. The target image and its constructors have not executed at this point. After release, `execve` preserves that PID/group, literal arguments and standard streams.

This is an internal component. The current public native launch command does not use it yet. A versioned public native build/preparation/execution contract, host integration in both lanes, independent oteleq reconstruction and final language qualification remain work. This component does not establish the executed image's complete source origin or dependency closure.

## Host API

```python
from pathlib import Path
from otelc import native_gate

product = native_gate.build(Path('/absolute/new-tool-output'))
waiting = native_gate.start(product, ('/absolute/application', '--literal-argument'),
                            cwd=Path('/absolute/application-root'))
try:
    ready = waiting.ready()  # Observe only; no target constructors have run.
    # The consuming host performs its independent ownership checks here.
    waiting.release()
    waiting.process.wait()
finally:
    waiting.cancel()
```

The caller-held product rechecks source, compiler and executable bytes and file facts. Target bytes/facts are checked again immediately before acknowledgement. Edited retained JSON cannot replace these bindings. A release before readiness, repeated release, changed target, rejected acknowledgement or expired wait does not start the target. An unreleased helper is cancelled by closing its private pipe and reaped before process-group probes. The default host wait is 30 seconds; explicit integer budgets from 1 through 300,000 milliseconds are accepted.

The helper itself starts with a minimal environment. The original target environment travels through an anonymous owned file descriptor and is supplied only to the released image. This preserves loader settings such as `LD_PRELOAD` and `DYLD_INSERT_LIBRARIES` without running application-library constructors inside the helper. Environment contents are not written to retained build records. Standard application input does not carry the release capability.

## Build context and checks

The builder uses the matched LLVM 22 C compiler with fixed C11 flags. It retains the actual command, source/compiler/product identities and compilation database. Coverage builds save their counters before exec, since exec does not run the helper's normal exit handlers.

`tests/test_native_gate.py` checks real application and loader-library constructors, PID/group continuity, literal arguments, stdin/stdout, cancellation, timeout, changed targets, malformed commands and release refusal. It requires at least 80% native C line coverage and retains the actual executable map, raw merged profile, LCOV and compiler context. The default Python gate separately requires 80% coverage for the host module. Sonar imports this C source as the fifth production unit alongside the existing four compiler/SDK units.
