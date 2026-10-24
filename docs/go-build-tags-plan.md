# Explicit Go build tags

<!-- markdownlint-disable MD013 -->

Status: candidate implementation. The public CLI and Python API produce explicit tagged build-5 records with inspection 2 and compiler-free launch validation. Focused actual stock/woven, live-control and altered-evidence checks pass. Full final checks, independent review and consumer admission remain required. Untagged calls retain build 4 and inspection 1.

## Expected behaviour and acceptance

Build tags are compiler inputs. An explicit tag set must select the same original files for source/type inspection, the stock binary and the woven binary. Instrumentation must not edit developer sources or inherit ambient `GOFLAGS`. The common instrumentation TOML remains unchanged; callers supply their original build context separately.

The opt-in public interface is repeated `go-build --build-tag NAME`, with `go-protocols --build-tags` for tooling discovery. No tags keeps build 4 and inspection 1, including their existing closed record shapes and compiler recipes. Explicit tags select build 5 and inspection 2, with the canonical sorted, deduplicated nonempty `build_tags` list retained in both records. Existing runtime config 2, health 4 and prepared request/plan/command/execution 1 remain bound to the exact build identity. There is no version fallback.

Tag grammar comes from the pinned Go 1.27.3 `go/build/constraint` parser. A name must parse as one exact `TagExpr`, rather than an expression, option or whitespace-separated list. Unicode letters/digits are valid compiler tags; Python Unicode tables must not define this contract. The inspector requires an already canonical set of at most 128 names, each at most 256 UTF-8 bytes. SDK/CLI input normalisation retains exact spelling and produces the same set before invoking inspection.

The private tooling pin must remain excluded from the selected application build even when the user supplies `tools`. For build 5, its constraint is the negation of the canonical first tag. That tag is explicitly passed to both builds, so the pin cannot become application code. Synthetic Go checks retain its dependency import through `go mod tidy` and successfully build with `feature`, `tools`, `go1.27` and `π`. The actual SDK/weaver checks also build with `tools` alone and with `feature`, `tools` and `π` together. The old `tools`-guarded pin fails for an otherwise valid stock `tools` build. The `ignore` tag fails in both stock and private synthetic builds because it activates standard-library generator sources; such compiler failures remain failures, with no fallback or success record. Independent consumer qualification remains required.

Build 5 additionally retains `command_evidence`: exact file identities for `commands.json` and every `command-N.stdout` and `command-N.stderr`. Launch validation checks the complete command-log denominator, returned command values and both final compiler recipes, including the one literal `-tags=...` flag. Altered logs, changed tags and rehashed substitutions fail before launch. These records are consistency evidence, not signed compiler attestation; an independent consumer must bind its requested build context and active input/package maps before compilation.

## Commands

```sh
bin/quux-otelc go-protocols --build-tags
bin/quux-otelc --config examples/otelc.toml go-build \
  --root /absolute/path/to/module --target ./cmd/app \
  --build-tag feature --build-tag tools --output build/tagged-app
bin/quux-otelc --config examples/otelc.toml go \
  --build build/tagged-app --runtime build/tagged-runtime
```

Use fresh build/runtime directories. The Python build API accepts `build_tags=("feature", "tools")`; `prepared_go.protocols(build_tags=True)` discovers the opt-in format. Preparation and execution validate the recorded tags without calling a compiler. Tags change compilation inputs, so changing tags requires a new paired build. Metric switches remain live controls and do not require that rebuild.

Focused acceptance must cover alternate tagged implementations, multi-tag constraints, excluded invalid source, Unicode tag spelling, invalid/duplicate/oversized tags, unknown request fields, unchanged originals and no application initialiser execution. Both final binaries must use the same explicit literal `-tags=...` argument and produce independently expected application values and decoded telemetry. Retained compiler/package/module/active-input maps must match the caller's requested graph. Altered tag records, command logs, source inventories or compiled products must fail acceptance.

## Evidence so far

The maintained inspector regression first failed with `unknown field "build_tags"`. The corrected inspector passes its actual Go race suite with 90% statement coverage. It selects the expected original default/feature files under one/two tags, ignores an excluded syntactically invalid file, preserves all original bytes, accepts Go Unicode tag characters and refuses expression/flag/noncanonical requests before root/compiler inspection. A legacy request still emits schema 1 without a `build_tags` field.

Ten focused Python checks exercise actual public CLI builds, combined Unicode tags, the single `tools` case, unchanged sources, active embedded data, ignored ambient `GOFLAGS`, invalid Unicode names, all four startup states and live transitions in the same application PID. Decoded telemetry retains five selected function spans and three final collected calls per schedule. Altered and rehashed tag/inventory/command evidence is refused. Existing untagged regressions, full checks and independent review are tracked separately; focused evidence does not establish consumer admission. The existing source-completeness, cgo/workspace/vendor and broader Go qualification limits are unchanged; this work does not turn compiler inspection into whole-program origin proof.
