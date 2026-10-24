# Explicit Go build tags

<!-- markdownlint-disable MD013 -->

Status: implementation in progress. The isolated compiler inspector accepts schema-2 requests and returns schema-2 inventories with explicit tags. The public SDK still exposes its reviewed untagged build-4 path. Build-5 production, public CLI flags, launch validation and independent consumer admission are not implemented yet.

## Expected behaviour and acceptance

Build tags are compiler inputs. An explicit tag set must select the same original files for source/type inspection, the stock binary and the woven binary. Instrumentation must not edit developer sources or inherit ambient `GOFLAGS`. The common instrumentation TOML remains unchanged; callers supply their original build context separately.

The planned opt-in public interface is repeated `go-build --build-tag NAME`, with `go-protocols --build-tags` for tooling discovery. No tags keeps build 4 and inspection 1, including their existing closed record shapes and compiler recipes. Explicit tags select build 5 and inspection 2, with the canonical sorted, deduplicated nonempty `build_tags` list retained in both records. Existing runtime config 2, health 4 and prepared request/plan/command/execution 1 remain bound to the exact build identity. There is no version fallback.

Tag grammar comes from the pinned Go 1.27.3 `go/build/constraint` parser. A name must parse as one exact `TagExpr`, rather than an expression, option or whitespace-separated list. Unicode letters/digits are valid compiler tags; Python Unicode tables must not define this contract. The inspector requires an already canonical set of at most 128 names, each at most 256 UTF-8 bytes. SDK/CLI input normalisation will retain exact spelling and produce the same set before invoking inspection.

The private tooling pin must remain excluded from the selected application build even when the user supplies `tools`. For build 5, the proposed private pin constraint is the negation of the canonical first tag. That tag is explicitly passed to both builds, so the pin cannot become application code. Synthetic Go checks retain its dependency import through `go mod tidy` and successfully build with `feature`, `tools`, `go1.27` and `π`. The old `tools`-guarded pin fails for an otherwise valid stock `tools` build. The `ignore` tag fails in both stock and private builds because it activates standard-library generator sources; such compiler failures must remain failures, with no fallback or success record. This pin recipe still needs qualification with the actual SDK/weaver and independent consumer.

Focused acceptance must cover alternate tagged implementations, multi-tag constraints, excluded invalid source, Unicode tag spelling, invalid/duplicate/oversized tags, unknown request fields, unchanged originals and no application initialiser execution. Both final binaries must use the same explicit literal `-tags=...` argument and produce independently expected application values and decoded telemetry. Retained compiler/package/module/active-input maps must match the caller's requested graph. Altered tag records, command logs, source inventories or compiled products must fail acceptance.

## Evidence so far

The maintained inspector regression first failed with `unknown field "build_tags"`. The corrected inspector passes its actual Go race suite with 90% statement coverage. It selects the expected original default/feature files under one/two tags, ignores an excluded syntactically invalid file, preserves all original bytes, accepts Go Unicode tag characters and refuses expression/flag/noncanonical requests before root/compiler inspection. A legacy request still emits schema 1 without a `build_tags` field.

Public build/launch and consumer evidence remain required. The existing source-completeness, cgo/workspace/vendor and broader Go qualification limits are unchanged; this work does not turn compiler inspection into whole-program origin proof.
