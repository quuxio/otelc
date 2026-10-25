# Private Rust compiler component

<!-- markdownlint-disable MD013 -->

This component discovers original Rust function definitions and inserts typed synchronous MIR entry/normal/unwind probes into a private build. Application source files contain no instrumentation imports or guards. The injected bridge uses the existing official C++ SDK pipeline; it does not add another exporter.

The development profile uses **Rust 1.98.1 and LLVM 22.1.8**, with the matching `rustc-dev` component. Actual compiler/SDK execution has been checked on macOS ARM64. The gate also accepts 64-bit GNU/Linux hosts; hosted execution must pass before that platform is described as verified. Compiler-private APIs require this exact toolchain. Bind `RUSTC` explicitly because selecting Cargo through rustup does not prevent a preceding Homebrew compiler from being selected.

```sh
rust_compiler=$(rustup which --toolchain 1.98.1 rustc)
rust_sysroot=$(rustup run 1.98.1 rustc --print sysroot)
export RUSTC="$rust_compiler"
export RUSTC_BOOTSTRAP=otelc_rust_compiler
export RUSTFLAGS="-C prefer-dynamic -L native=$rust_sysroot/lib -C rpath -C link-arg=-Wl,-rpath,$rust_sysroot/lib"

rustup run 1.98.1 cargo build --offline --locked \
  --manifest-path native/rust/compiler/Cargo.toml \
  --target-dir build/rust-compiler
rustup run 1.98.1 cargo test --offline --locked \
  --manifest-path native/rust/compiler/Cargo.toml \
  --target-dir build/rust-compiler-tests
"$rust_sysroot/bin/cargo-clippy" clippy --offline --locked --all-targets \
  --manifest-path native/rust/compiler/Cargo.toml \
  --target-dir build/rust-compiler-tests -- -D warnings
```

`OTELC_RUST_REQUEST` identifies a closed version-one request. It binds a canonical application root, exact original entry, source SHA-256 catalogue, new inventory output outside the application root, inspection/weaving mode and selected compiler identities. Common-policy tooling resolves the selection; the component does not introduce a separate instrumentation policy. Both modes check the actual compiler crate root. Discovery compares original source bytes with the compiler's loaded source hash. Weaving injects only the private bridge import and selected typed MIR calls. Async and macro-expanded selected bodies are explicitly refused until their completion profiles exist.

The separate internal [automatic lifetime component](../../../docs/rust-lifetime-weave.md) uses request version 2 with `mode="lifetime-weave"`, explicit function/local/resolved-type bindings and complete lifetime inventory fingerprints from common-policy discovery. Discovery uses the same compiler flags as weaving; the compiler rechecks each complete body fingerprint before adding hooks. It inserts root-local construction, move and drop observations after compiler drop elaboration. Existing version-one requests cannot carry those bindings. This component requires genuine unchanged-source compiler/SDK execution. The separate [public lifetime candidate](../../../docs/rust-public-lifetimes.md) connects supported selections to the standalone build and launcher.

Lifetime inspection now emits inventory version 2 with explicit unavailable foreign, owned-container and opaque root values. Borrowed reference tuples are not owned object containers. Common-policy request generation refuses selected unavailable values, and complete snapshot hashes retain even excluded rows. Ordinary function inspection/weaving keeps its existing version-one inventory; this discovery change does not add a runtime boundary for unsupported types.

Invoke Clippy through its absolute toolchain path so Cargo cannot select another installed `cargo-clippy`. `RUSTC` alone does not select it. Automatic `rpath` also retains the matching Rust standard-library directory for dynamically linked build scripts; the explicit sysroot path supplies the compiler-private libraries.

The maintained unit tests check request shape/types, source hashes, path aliases, existing outputs, source preservation and size/selection bounds. These tests do not invoke an application or prove MIR instrumentation. Separate actual stock/woven execution and decoded SDK evidence are required for that boundary.

Run the maintained compiler/SDK integration gate with an environment containing the locked Python dependencies:

```sh
make rust-compiler-check PYTHON=/absolute/path/to/python
```

This component target is part of `make check` and the required CI jobs. It builds a genuine SDK from the current checkout, the pinned compiler and bridge, then runs unedited module/loop/panic fixtures at optimisation levels 0 and 3. Eight actual application processes exercise all four metric startup states and live switches. It compares stock values, drop side effects and default panic output, retaining raw stderr and excluding only the per-process thread ID from that comparison. The original panic payload is checked by the ordinary application. Unsupported async/macro selections, wrong original entries, unbound modules and incompatible bridge definitions/signatures must refuse without a compiler crash. The common health reader accepts Rust function accounting and refuses unsupported C++ destruction/C-free claims. LLVM and Python component floors are enforced at 80%; retained sources, products, commands, diagnostics and coverage remain in a new `build/rust-compiler-check-*` directory.

The corpus also retains four private drop, four private key and eight automatic root-local lifetime application runs. Both stock and woven corpus executables use `-C instrument-coverage`, so the final Rust profiler runtime records the bridge counters. Library-only coverage produced zero release-hook counts despite real SDK receipt. These are behavioural coverage fixtures; the separate public benchmark build keeps its ordinary flags.

The gate publishes repository-relative `build/rust-coverage.lcov` and actual compiler/bridge Clippy JSON in `build/rust-clippy.json`. It checks all seven production Rust modules, their measured coverage and retained source/report identities before Sonar analysis. Python orchestration, report validation, original/discovery/automatic lifetime cases and health each retain their own 80% floor. Sonar imports these reports using its [Rust analysis properties](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/languages/rust); it does not run Clippy on behalf of CI. These reports do not replace the existing native launcher coverage checks.

An existing genuine SDK from the same unchanged checkout can be selected with `OTELC_NATIVE_SDK_BUILD=/absolute/path/to/sdk`. The target rechecks its source/product record; it does not copy or repair another checkout's manifest. New builds require 8 GiB free. The pinned Rust toolchain, `rustc-dev`, Clippy and locked Cargo dependencies must already be installed/cached for the offline commands above.

This remains an internal development component. The standalone [public lifetime candidate](../../../docs/rust-public-lifetimes.md) connects supported drop selections and versioned health. Prepared host release, complete compiler-input closure, broader object/resource ownership, async/context integration, actual performance/dashboard evidence, passing hosted gates and independent oteleq qualification remain required before Rust language admission. See the [rebuild acceptance criteria](../../../docs/rebuild-plan.md).
