# Go private-bootstrap build candidate

The explicit `--scope-safe` route builds original application sources with the common TOML policy. A generated private package owns SDK initialisation, boolean constants, type aliases and nil-result observation. Injected application helpers use reserved names and qualified private-package types. This prevents legal application bindings such as `nil`, `any`, `int`, `bool`, `panic`, `true` or `false` from changing the probes' meaning.

The runtime accessor also avoids a same-package runtime variable that can still be uninitialised during an interface-dispatched application package initialiser. Error observation preserves the result's static type: a nil pointer is absent, an error interface containing that pointer is present, and a non-comparable error value does not introduce an interface-comparison panic. Observation does not call the application's `Error()` method.

## Explicit tooling and build selection

Install the [verified private weaver](go-scope-tooling.md) into a new directory, then reuse it read-only:

```sh
python -m ci.build_go_upstream --go /absolute/path/to/go \
  --scope-safe --output /absolute/path/to/private-weaver

bin/quux-otelc --config examples/otelc.toml go-build \
  --scope-safe --prepared-tools \
  --tooling-directory /absolute/path/to/private-weaver \
  --go /absolute/path/to/recorded/go \
  --root /absolute/path/to/application \
  --output /absolute/path/to/new-build
```

Use the compiler path recorded by installation, which must be native Go 1.27.3. Application build output must be outside the original source directory. `--build-tag feature` selects the tagged profile; repeat the flag for more literal tags. All flags apply to both the original-source baseline and the private instrumented build. The original application sources and module graph stay unchanged.

Without `--prepared-tools`, this route creates its tooling in `build/go-upstream-scope`, or the explicit `--tooling-directory`. The first build installs and validates it. Use `--prepared-tools` for later builds; an existing private source installation is not overwritten.

Launch a retained build through the ordinary compiler-free command:

```sh
bin/quux-otelc --config examples/otelc.toml go \
  --build /absolute/path/to/new-build \
  --runtime /absolute/path/to/new-runtime
```

The [live control commands](go-source.md) apply without rebuilding or restarting the process. Tracing stays independent of metric collection and emission.

## Consumer format contract

Negotiate without a compiler, application or readable configuration:

```sh
bin/quux-otelc go-protocols --scope-safe --prepared-tools
bin/quux-otelc go-protocols --scope-safe --prepared-tools --build-tags
```

| Field | Untagged | Tagged |
| --- | --- | --- |
| `build` | 6 | 7 |
| `inspection` | 1 | 2 |
| `helper_profile` | `private-bootstrap-v1` | `private-bootstrap-v1` |
| `prepared_tools` | 2 | 2 |
| `config` / `health` | 2 / 4 | 2 / 4 |

Build 6 adds `helper_profile` and `command_evidence` to the build-4 field set. Build 7 adds the same helper field to the tagged build-5 field set. Both retain actual command/stdout/stderr identities and checked stock/woven compiler recipes. Tagged inspection and build records retain the canonical nonempty tag list. The toolchain requires schema 2, the fixed `raw-empty-globals-v1` backport and its explicit private version. Public preparation rechecks original sources, private inputs and binaries without invoking Go.

These are explicit candidate formats. Default discovery remains build 4/5 and prepared tooling 1; a tooling-2 installation cannot silently build an old format. Independent oteleq reconstruction, provider admission and complete Go language qualification remain outstanding. Every route still reports `source_complete=false`.

## Regression scope

The maintained public-CLI corpus covers seven package-level predeclared-name shadows, nine error-result shapes, interface-dispatched initialisation and a generated allocation/release wrapper. It exercises stock and woven execution, both tag profiles, compiler-free launches, all four live metric states in one process, exact function status/call/error observations and resource spans. Broader application/dependency closure and language admission require separate evidence.
