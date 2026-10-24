# Native Python SDK code joins

<!-- markdownlint-disable MD013 -->

This optional contributor component connects the Python monitor's selected code IDs to the [native origin catalogue](python-loader-origins.md). It adds no application annotations or instrumentation calls. Public launches still negotiate journal 1 and report `source_complete=false`; the explicit [public origin profile](python-prepared-origins.md) adds both-lane supervisor integration and journal-2 negotiation. Independent provider admission remains separate work.

## Identity boundary

The native `_otelc_inputs.observe` function first authenticates the bound SDK callback, the actual executing frame, the same code object and its genuine `RESUME` offset. Its existing descriptor record gives the selected code a process-local SDK ID. Before returning that ID to the monitor, the optional origin observer requires the very same live code object in its weak catalogue. Equal descriptors, filenames or source bytes cannot supply membership.

The native and SDK monitoring callbacks can run in either order. An authenticated SDK callback can publish the catalogue member's first execution before writing its join. A later native callback does not duplicate that execution. A prior native execution is reused. The join identifies the particular SDK code and catalogue execution; the journal reader independently checks their path, offset and complete code graph. The monitor carries the returned code ID into ordinary function spans and metrics.

Only one join is written for a selected code identity. Repeated calls and worker calls reuse it. Missing catalogue membership refuses the code identity and marks capture incomplete; instrumentation still preserves the application's result. Code created by an unrelated `compile`, including identical code with the same filename, cannot borrow another object's origin. A manually invoked Python callback also fails the native start check. A first SDK observation at a later generator-resume offset refuses the join and marks capture incomplete, preserving a valid partial journal. It cannot rewrite the earlier native start to match. Journal-1 launches have no origin observer and keep their existing descriptor behaviour.

## Contributor checks

Use the selected CPython environment and native toolchain:

```sh
PYTHONPATH=tests .venv/bin/python -m unittest -v \
  test_python_origin_sdk_joins test_python_code_inputs \
  test_python_entry_origins test_python_loader_origins
make python-inputs-check
make check
```

The tests require actual native starts and verify both callback orders from retained record sequences. They also check repeated IDs, equal-code refusal and forged callback refusal. A separate actual `PythonMonitor`/`PythonRuntime`/SDK test decodes received OTLP and requires the joined code ID on both main-thread and worker-thread spans, counters and histograms. That test is a component harness; it does not qualify the public launcher or every source-loading mechanism.

Existing per-file native 80% coverage floors remain in force. Cached bytecode, custom loaders, dynamic code origin, arbitrary interpreters/forks, installed/native dependencies and whole-program closure retain their documented unsupported or unqualified boundaries. A successful join proves the recorded selected code relation, not complete application provenance.
