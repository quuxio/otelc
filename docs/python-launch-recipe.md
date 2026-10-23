# Python native launch request component

<!-- markdownlint-disable MD013 -->

`otelc.python_recipe` is the request/argv layer for the candidate [native input component](python-input-journal.md). The public [prepared launcher](python-prepared-launch.md) uses it for native app/SDK ownership and post-drain rechecks. Complete import/code evidence and independent provider admission remain outstanding. Python remains a candidate.

The original request keeps the complete schema-3 common policy, its original TOML read when available, script or module, arguments, caller cwd, requested/resolved paths, interpreter file/version/cache tag and declared environment inputs. Resolution produces a separate plan; changing the plan cannot change the retained request. Script paths resolve from the application root. Root/runtime paths resolve from the caller cwd. String inputs retain relative, Unicode and symlink spelling; a caller that already converted a string to `Path` has already applied that class's lexical normalisation.

`resolve_plan` derives the ordinary native script/module arguments. `native_command` adds only the independently verified native driver and an owned inherited journal descriptor. These functions accept no command or argv field from execution evidence. The caller must retain original intent independently of a receipt and verify the native build manifest before selecting the driver.

Homebrew framework Python can substitute its framework binary into `sys.orig_argv[0]` while preserving a venv `sys.executable`. A fixed `-I -S` probe observes that value without running application startup hooks or reading application import paths. The request retains it separately from the selected interpreter path, so the native driver preserves both values. CPython documents [script/module argument behaviour and isolated mode](https://docs.python.org/3.14/using/cmdline.html).

`verify` rechecks the derived plan, original path resolution, interpreter file/metadata, policy source and the declared environment snapshot. Its owner must invoke it before launch and after actual SDK drain, using the original environment before any bootstrap injection. It does not prove that an SDK drained, imports/code were captured, or telemetry reached a collector. The resolved plan explicitly reports `input_scope=native-source-entry-only` and `source_complete=false`.

The environment record is an allowlist of Python launch/import/encoding/runtime settings, locale, timezone and temporary-directory settings. Unset and explicitly empty values differ. It is not a complete application environment or native-loader closure. Credentials and unrelated environment values are excluded. Arbitrary interpreter flags, `-c`, stdin/directory/zip entries and bytecode-only script entries are outside this component's fixed profile. Existing script/module developer sources are unchanged.

Run the focused component checks in the prepared environment:

```sh
PYTHONPATH=tests .venv/bin/python -m coverage run --branch \
  --source=otelc.python_recipe -m unittest test_python_recipe -v
.venv/bin/python -m coverage report --fail-under=80
make check
```

The tests compare actual native script/module raw channels, exits, frames, arguments and public entry metadata. They also cover request/plan isolation, independently disabled metric flags, policy/path/interpreter/environment changes, malformed requests, descriptor bounds and an application site hook which the metadata probe must not run. Entry-byte capture remains the native component's separate responsibility; module execution cannot invent a complete source receipt.
