# Go raw-rule backport component

Application packages can legally shadow predeclared Go names. Instrumentation must preserve their results, errors and output. The pinned official Go weaver emits an unused `HookContext` declaration for raw-only rules; that declaration uses names such as `int` and `bool` in application scope and can prevent an otherwise valid application from compiling.

The explicit `raw-empty-globals-v1` backport skips this declaration only when there are no trampoline variable declarations. Function-rule packages still receive their required globals and `HookContext` API. This component does not change the embedded official instrumentation bundle.

## Build and retain the private tool

```sh
python -m ci.build_go_upstream --go /absolute/path/to/go \
  --scope-safe --output /absolute/path/to/new-tool-installation
```

The installer requires native Go 1.27.3, verifies the pinned upstream v1.0.1 module and bundle, and copies the module into the installation's fixed `source` directory. It changes one file only after checking its exact original SHA-256. It checks the resulting file SHA-256 and all copied source files before retaining `scope-source.json`. It never patches the shared module cache. An existing `source` destination is refused so that an earlier installation or failed attempt remains available for inspection.

The executable reports `otelc version v1.0.1+quux.scope.1`. Its actual Go build metadata retains `(devel)` for the private source build. The installer does not label that binary as an unmodified official release. `toolchain.json` schema 2 records the backport profile, source-manifest identity, actual build metadata, reported version, compiler and binary identities. The existing default installer remains the unmodified upstream schema-1 route.

## Read-only validation and scope

`otelc.go_prepared_tools.prepare()` accepts this explicit schema-2 installation, rechecks the fixed source directory and manifest, and queries only the recorded compiler and fixed `bin/otelc`. The caller-held binding detects changed source bytes, file facts, tool identities or metadata. Application build caches do not select replacement installed tools. Validation does not download, install or rewrite the tooling.

This is an installer and prepared-tool component. The separate explicit [private-bootstrap candidate](go-scope-build.md) negotiates application builds 6/7 and requires a caller-held tooling-2 binding. It does not establish complete Go language qualification. Independent oteleq reconstruction and provider admission remain outstanding.

## Checks

`tests/test_go_scope_patch.py` covers exact-source patching, copy preservation, source drift, changed manifests and refused links. `tests/test_go_scope_tooling.py` checks the explicit version/provenance contract and read-only bindings. Its real-compiler test demonstrates that the raw-only regression fails on the original source and passes after the patch, runs the upstream instrument package tests, and confirms that required trampoline declarations remain present.

The default Python test discovery includes these checks. The Makefile requires at least 80% line coverage for the patch component, installer and prepared-tool reader separately. The private-bootstrap candidate has separate application-level helper/error-result regressions; independently reviewed provider admission remains work.
