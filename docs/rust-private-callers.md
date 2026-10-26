# Original-module discovery for private Rust callers

Status: internal compiler component under qualification. The explicit
`module-inspect` mode is the first prerequisite for compiler-owned private
callers. It does not inject helpers, call private functions, construct private
receivers or change the public source-build 1/2 contracts. No application or SDK
is started by module inspection.

The pinned Rust 1.98.1 compiler reports its resolved HIR modules after normal
parsing, cfg selection and type checking. Each record retains the compiler path,
parent path, original declaration and body filenames, physical byte intervals,
crate/inline/external kind and caller-route availability. Empty external modules
keep their zero-byte body. Expanded and block-local routes remain visible and
unavailable; a child cannot acquire access through an unavailable ancestor.
Original source hashes must agree with the compiler's loaded bytes. The
inventory is an exclusive, bounded output outside application source.

## Component command

Use the compiler component built by `make rust-tools`, or the instrumented
compiler from the focused workflow. Write a separate request outside the
application source root:

```json
{
  "schema_version": 1,
  "mode": "module-inspect",
  "root": "/work/original",
  "entry": "/work/original/main.rs",
  "output": "/work/evidence/modules.json",
  "sources": {"/work/original/main.rs": "SHA256_OF_ORIGINAL_FILE"},
  "selected": []
}
```

```sh
OTELC_RUST_REQUEST=/work/evidence/request.json \
  /work/rust-tools/compiler/debug/otelc-rust-compiler \
  /work/original/main.rs --crate-name application --edition=2024 \
  --sysroot /path/to/pinned/sysroot -o /work/evidence/unused-image
```

Successful inspection writes `otelc.rust.module-inventory` schema 1 with
`byte_coordinates="original-utf8"` and `source_complete=false`. It stops before
code generation and must not create the requested image. Byte intervals refer
to the physical original UTF-8 bytes, including BOM/CRLF offsets; they are not
character or normalised-source offsets. The tooling reader
`otelc.rust_modules.read(path, root, entry, sources)` requires the caller-held
original catalogue and rechecks its bytes. Reading a statement does not prove
that the compiler ran or establish complete input provenance.

## Checks and remaining work

`make rust-module-check` requires explicit `OTELC_RUSTC`,
`OTELC_RUST_COMPILER` and `OTELC_RUST_COMPONENT_OUTPUT` inputs. It runs genuine
compiler inspections and literal refusal tests, enforcing separate 80% Python
floors. The focused compiler workflow also requires 80% Rust module coverage;
the full gate includes this module in the closed Rust analysis catalogue.
No test application is executed by this component.

Cases cover nested `foo.rs` and `foo/mod.rs`, inline/external children, path
attributes, empty files, macro and block-local modules, inactive and enabled
missing cfg modules, Unicode/BOM/CRLF physical ranges, selected-function refusal
and changed original hashes. Hosted pinned-compiler execution and protected
checks are required before these are reported as verified.

Next work must bind separate physical helper inputs and original module routes,
preserve resolved original callees/types/method and macro resolution, and keep
helpers unselected even under wildcard policies. Generated observers must not
call application formatters or capture shadowed builtins. Receiver callers must
use declared existing factories and bind original fields and aliases. Plain and
woven diagnostic callers must use the same helper transformation; a stock
application build remains a separate control. Original developer files and
visibility stay unchanged. No new public caller profile is admitted by this
component. Cargo/dependencies, async callers, full source closure, independent
oteleq qualification and shipping remain work.
