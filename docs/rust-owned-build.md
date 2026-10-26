# Rust owned-build component

<!-- markdownlint-disable MD013 -->

This component binds an existing standalone Rust source build to the fixed native pre-exec gate. It is the build boundary for the planned owned Rust launcher. It does not yet supply preparation, a supervisor, host release or an execution record. The existing direct `rust-run` command is unchanged. No application source changes are required.

The separate `rust-owned-build.json` envelope has schema1, kind `otelc.rust.owned-build` and profile `rust1981-owned-standalone-v1`. It retains the original `rust-source-build.json` identity and source profile: sync-v1/source-build1 or drop-v2/source-build2. Original source, compiler, bridge, SDK, inventory and product records remain in their existing locations. The envelope does not copy or rewrite them, and `source_complete=false` remains explicit.

```python
from pathlib import Path
from otelc.config import Policy
from otelc import rust_build, rust_owned_build

policy = Policy.load(Path("examples/functions.toml"))
original = rust_build.read(Path("/work/rust-compiled"), policy)
owned = rust_owned_build.build(policy, original, Path("/work/rust-owned"))
checked = rust_owned_build.read(owned.directory, policy, source=original)
rust_owned_build.verify(checked, policy)
```

Creating the envelope compiles only the fixed C11 exec gate using LLVM22 and requires at least 8 GiB free. Its new private output must sit outside the original application source/build, compiler/SDK build, SDK tooling/native source, selected Rust sysroot, LLVM installation and original source/tool input directories, including physical aliases. Normal output under the SDK checkout's separate `build/` directory remains supported. Existing output directories refuse. Failure leaves diagnostic files in the new directory but does not publish a valid envelope.

Reading or verifying an envelope starts no compiler or application. Pass the caller-held `source` to require that exact original build. Without it, `read` loads the operator-selected envelope's referenced canonical, fixed-name source-build record, then validates the complete original build. It never executes recorded compiler commands or metadata-selected modules. The gate path, source and compilation recipe are fixed; original file identities and held bytes are rechecked. Nested identities retain exact JSON primitive types: `true` and `1.0` cannot replace the integer `1` in file facts.

`make rust-owned-check` runs bounded literal envelope/refusal tests without compiling applications. `make rust-public-check` also runs a genuine compiler/SDK test for both original source profiles. Each target has a separate 80% component coverage floor. The genuine test builds the gate, then prohibits process creation while reading and verifying it. Hosted records retain the envelopes, gate records and small gate images. Literal tests alone do not establish a genuine build or execution capability.

The prepared supervisor, actual pre-exec OS ownership, host release, final health, raw downstream telemetry and independent oteleq execution qualification remain required. Cargo/build scripts/dependencies, general ownership/async, complete compiler/sysroot closure and shipping equivalence remain separate open requirements.
