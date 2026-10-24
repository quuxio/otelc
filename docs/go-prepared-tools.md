# Reuse prepared Go tooling

<!-- markdownlint-disable MD013 -->

The explicit prepared-tools profile separates installing SDK compiler tooling from compiling an application. The ordinary `go-build` path still prepares the official weaver through the existing setup recipe. Tooling consumers that freeze an SDK first must opt into reuse so an application's independent module cache cannot rebuild or replace the installed weaver or change its recorded bundle path.

## Prepare, negotiate and build

```sh
make go-upstream
bin/quux-otelc go-protocols --prepared-tools
bin/quux-otelc --config examples/otelc.toml go-build \
  --prepared-tools --go /absolute/recorded/go-root/bin/go \
  --root /absolute/path/to/module --output build/prepared-app
bin/quux-otelc --config examples/otelc.toml go \
  --build build/prepared-app --runtime build/prepared-runtime
```

The compiler path must be the exact compiler recorded by setup in the SDK's fixed `build/go-upstream/toolchain.json`. This mode does not substitute the toolchain manager or silently install a missing release. Fresh output/runtime directories and the existing native Go 1.27.3 profile are still required.

`go-protocols --prepared-tools` adds the explicit integer capability `prepared_tools: 1`. Default discovery remains unchanged. Combining it with `--build-tags` negotiates the separate [tagged build-5 profile](go-build-tags-plan.md); repeated `go-build --build-tag NAME` arguments compose with `--prepared-tools`. Reuse does not change build-4/build-5 record shapes or any runtime/prepared format. Consumers must require the requested capability and must not fall back to reinstalling tools.

## Read-only installation boundary

The SDK reads its fixed manifest and checks the selected compiler, fixed `build/go-upstream/bin/otelc` and recorded pinned bundle. It validates the native compiler/root/platform, exact module/checksum/build metadata and reported weaver version. Only three metadata commands run: compiler environment, compiler build-info inspection of the fixed weaver and the fixed weaver's version. The validation path does not run installation or module-download commands and writes no installer error log.

An immutable caller-held binding retains the manifest, compiler, weaver and bundle bytes and file facts. The builder rechecks it before creating application output and after compilation, before publishing `build.json`. Changed bytes, replaced files, changed metadata and edited nested tool records fail. These checks preserve a selected installation; they are not a signature or whole-program provenance claim.

Application and inspector builds still need compiler scratch and their own `GOMODCACHE`, `GOCACHE` and `GOPATH`. They may fetch checked application/SDK module dependencies into that cache. This is distinct from reinstalling the fixed SDK compiler wrapper. Developers do not change their application source or the common instrumentation policy to use this profile.

The Python API uses `go_prepared_tools.prepare(go, sdk_tool_directory)` to obtain a caller-held binding, `binding.toolchain()` for a fresh record, and `go_build.build_project(..., prepared_tools=binding)` for a checked build. Public CLI reuse always selects the SDK's fixed tooling directory; manifest values cannot select another executable path for the weaver.

## Qualification boundary

Focused checks cover missing/altered pins, duplicate fields, compiler/root/platform mismatches, executable identity, changed file facts, metadata mutation during validation, failed queries without SDK writes and offline version negotiation. The actual CLI regression uses fresh application caches and a read-only installed SDK for ordinary and tagged paired builds, original application output, decoded telemetry and a changed-tooling refusal before publication. Full final checks, independent consumer qualification and protected delivery remain required; Go remains a candidate until its broader acceptance criteria pass.
