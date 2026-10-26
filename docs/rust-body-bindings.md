# Original Rust body bindings

<!-- markdownlint-disable MD013 -->

The internal Rust 1.98.1 compiler adds read-only `binding-inspect` to request schema 1. The request requires an empty selection and the existing original entry/source catalogue. It emits no application image, injects no helper and does not enable a new public build or private caller profile.

The `otelc.rust.body-bindings` schema-1 result retains every original HIR body owner, including unselected and unsupported bodies. Each record has its original source identity and byte interval, an availability reason and a diagnostic numeric definition index. Ordinary synchronous, non-const, unexpanded functions and methods receive two separate fingerprints: the complete typed MIR before ordinary optimisation and the complete `TypeckResults`. The latter includes resolved methods, overloaded operations, node types, generic arguments and adjustments. Async, closure, const/static and expanded owners remain explicit unavailable records. An owner without caller-bound physical source fails the query rather than disappearing.

The algorithm is `rustc-1.98.1-stablehash-128-v1`: rustc's `StableHash` and `StableHashState` map definition and HIR identities through stable definition paths. Both values are exactly 32 lower-case hexadecimal characters. They are 128-bit change detectors, not cryptographic hashes or proofs of semantic equivalence. The diagnostic numeric index is excluded. Span hashing must stay enabled; `-Zincremental-ignore-spans` refuses. Comparison requires the same pinned compiler, target, crate identity and relevant flags, and unchanged original physical inputs. A compiler upgrade requires a separately versioned algorithm and qualification.

Run with explicit matching compiler products:

```sh
export OTELC_RUSTC="$(rustup which --toolchain 1.98.1 rustc)"
export OTELC_RUST_COMPILER=/absolute/path/to/otelc-rust-compiler
export OTELC_RUST_COMPONENT_OUTPUT=/absolute/path/to/new/evidence
make rust-binding-check
```

The focused hosted job builds the compiler, runs genuine compiler queries and enforces separate 80% Rust and Python component floors. Controls compare identical repeated queries, add an unrelated definition that changes numeric indices, rebind a same-spelled callee and change a receiver's concrete type/method. Original application sources remain unchanged and no target image is emitted or run. The full compiler/SDK gate includes the same cases and its closed source/coverage catalogue includes `bindings.rs`.

Before helper injection can be admitted, the consumer must independently obtain and seal the original query using its own held source/compiler/flag intent. A producer-supplied before/after pair is insufficient. Original owners must retain a complete denominator; helper owners need a separate closed exclusion set. This original-only query therefore reports an empty `helper_exclusions` and keeps `source_complete=false` and `semantic_complete=false`. Physical helper bindings, hygienic AST injection, private receiver/factory/field observation, macro/name-resolution preservation and genuine generated execution remain separate work.
