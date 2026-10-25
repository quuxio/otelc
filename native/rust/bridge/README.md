# Private Rust runtime bridge

<!-- markdownlint-disable MD013 -->

This internal component forwards compiler-owned function identities and normal/unwind completion to the existing official C++ SDK pipeline. It contains no Rust exporter, policy parser, heap registry or developer bootstrap. Developer applications must not import it manually.

The initial compiler profile is Rust 1.98.1 with LLVM 22.1.8 on macOS ARM64. Build the matching crate with:

```sh
rust_bridge_compiler=$(rustup which --toolchain 1.98.1 rustc)
RUSTC="$rust_bridge_compiler" rustup run 1.98.1 cargo build --offline --locked \
  --manifest-path native/rust/bridge/Cargo.toml \
  --target-dir build/rust-bridge
```

The explicit `RUSTC` binding matters when Homebrew's compiler precedes the rustup proxies on `PATH`. Selecting Cargo alone does not establish which compiler built the crate.

The new named-entry ABI copies the UTF-8 name during the call. Its identity is separate from a native function address. Both kinds share the native registration limit, completion accounting, live metric gates and tracing. The tooling must link the matching native SDK and initialise it before application code starts.

The function-only internal Rust runtime plan explicitly uses `schema_version=2` and `language="rust"`. The private drop profile uses plan 3 and health 5; see the [drop bridge contract](../../../docs/rust-drop-bridge.md). Its `drop_new` hook allocates an observation key independent of object address or size, for later compiler-owned lifetime insertion. Existing C/C++ plans retain their own versions; a Rust plan cannot substitute those contracts. These are SDK component formats, not public Rust source-build or preparation protocols.

This crate alone does not instrument source, bind compiler inputs, insert automatic object lifetime probes or admit Rust as a supported public adapter. Automatic lifetime compiler integration, full Rust execution/lifetime/benchmark checks and independent qualification remain work. See the [rebuild acceptance criteria](../../../docs/rebuild-plan.md).
