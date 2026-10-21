# Prepared Java applications

<!-- markdownlint-disable MD013 -->

Run an existing compiled application with the same schema-3 policy as the example. otelc does not compile it, edit source/classes, add application dependencies or require instrumentation bootstrap. The official agent transforms selected classes in memory. The build producer supplies the classpath, main class and original-source bindings. Automatic arbitrary build-system discovery remains future work.

## Manifest contract

Paths resolve relative to the manifest; absolute paths are accepted. Classpath order is retained. Each instrumentable class, including nested classes, needs a binding. Unbound dependencies can run but cannot receive application probes. The main class must be bound.

```json
{
  "schema_version": 1,
  "main_class": "example.Main",
  "classpath": ["classes", "lib/dependency.jar"],
  "bindings": {
    "example.Main": {
      "source": "src/example/Main.java",
      "policy_path": "src/example/Main.java",
      "source_sha256": "SHA256_OF_ORIGINAL_SOURCE_BYTES",
      "class_sha256": "SHA256_OF_CLASS_BYTES",
      "provenance": "Build producer and exact compiler invocation"
    }
  }
}
```

For a JAR class, hash the uncompressed `.class` member. `policy_path` is the original logical path matched by `[sources]`. Function identity is `binary.Class.method(argumentTypes)`, for example `example.Main.workload(int)`, using `javap` type names without spaces. Exclusions take precedence. The official selector groups overloads by method name: selecting only some overloads is refused before launch. Lifetime selectors match binary class names, including `$` for nested types.

Bindings are the producer's explicit compiler-to-source attestation. Matching hashes prove declared files are unchanged; they do not prove an arbitrary caller's compilation history. Use authoritative compiler/build metadata rather than guessing from a basename or `SourceFile` attribute.

The initial profile refuses missing/stale bindings, duplicate JSON keys/class definitions, symlinks inside classpath directories, implicit JAR `Class-Path`/indexes, and multi-release JARs. Classpath entry paths cannot contain separators (`:` here) or wildcards. Binary class-name segments need ASCII initials. This is an explicit classpath/system-loader profile; module paths, custom loaders, arbitrary JVM flags and ambient `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS` or `_JAVA_OPTIONS` are unsupported. Lifetime types retain the [final-class and single-constructor limits](lifetimes.md).

## Runnable example

The ordinary [Java example](../examples/java/BenchmarkApp.java) has no telemetry code. Compile it and bind this known single-source build:

```sh
make java-build
mkdir -p build/prepared/classes
javac -d build/prepared/classes examples/java/BenchmarkApp.java
.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

source = Path("examples/java/BenchmarkApp.java").resolve()
classes = Path("build/prepared/classes").resolve()
bindings = {}
for compiled in sorted(classes.rglob("*.class")):
    name = compiled.relative_to(classes).as_posix()[:-6].replace("/", ".")
    bindings[name] = {
        "source": str(source), "policy_path": "examples/java/BenchmarkApp.java",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "class_sha256": hashlib.sha256(compiled.read_bytes()).hexdigest(),
        "provenance": "javac -d build/prepared/classes examples/java/BenchmarkApp.java",
    }
manifest = {"schema_version": 1, "main_class": "examples.BenchmarkApp",
            "classpath": ["classes"], "bindings": bindings}
(classes.parent / "application.json").write_text(json.dumps(manifest, indent=2))
PY
.venv/bin/python -m otelc --config examples/otelc.toml java \
  --manifest build/prepared/application.json --runtime /tmp/otelc-java-$$
```

The CLI creates a new owned 0700 runtime directory. Its path must be short and contain no `:`, `,`, `=` or `%`. Enter `10`, `gc`, `lifecycle`, `throw` or `quit`, as for the built-in example. Use a new runtime path and `--plain` for a baseline run. Arguments after `--` go unchanged to the main method. The same policy applies; no agent-specific settings belong in it.

From another terminal, use `python -m otelc metrics --socket /tmp/otelc-java-SHELL_PID/control.sock status`, then `off`, `collection on` or `emission on`. Replace `SHELL_PID` with the shell PID used in the launch path. `process.json` records the actual Java PID. The in-flight-export barrier is unchanged. Use `make stack-up` and the [Grafana dashboards](http://localhost:3001/d/otelc-lifetimes) to view telemetry.

## Consumer API and evidence

```python
import subprocess
from pathlib import Path
from otelc import control
from otelc.config import Policy
from otelc.prepared_java import PreparedApplication, prepare

application = PreparedApplication.load(Path("build/prepared/application.json"))
runtime = Path("/tmp/otelc-prepared-consumer")  # fresh for each run
runtime.mkdir(mode=0o700)
launch = prepare(application, Policy.load(Path("examples/otelc.toml")), runtime)
launch.verify_inputs()
process = subprocess.Popen(launch.argv, stdin=subprocess.PIPE, text=True)
state = control.wait_ready(Path(launch.identity["control_socket"]), process.pid)
assert state["pid"] == process.pid
control.request(runtime / "control.sock", "off")
process.communicate("10\nquit\n", timeout=30)
assert process.returncode == 0
launch.verify_inputs()
```

`prepare(..., plain=True, arguments=(... ,))` gives the baseline command without an agent or controls. Both modes send JVM class-loading diagnostics to a separate `class-load.log`, preserving application stdout/stderr. Consumers own process cleanup and PID recording; the CLI records its PID automatically.

Use `java --prepare-only` with the same CLI arguments for a JSON preparation route in the provider's own Python environment. It creates `launch.json`, prints the same identity JSON (including `argv` and `control_socket`), and starts no application JVM. The JDK version query still runs. A consumer can execute that argv with its own bounded process driver without importing otelc into its host environment. Loaded-origin evidence is available only after that child runs.

`launch.json` records argv, ordered classpath, source/compiler bindings, file hashes/sizes/inodes/mtimes, JDK version/binary, policy digest, and instrumented agent/extension/generated-configuration identities. `class-load.log` records actual JVM loaded-class origins separately from the static inventory. The generated `agent.properties` retains the resolved official method selector; `launch.json` binds its hash.

Verify inputs before launch and after execution. The CLI verifies after termination automatically; a mismatch is an error. This detects persistent changes, new classpath files and replaced inputs. It is not a filesystem snapshot or protection against inputs that change and are restored during execution. Keep inputs stable throughout the run. Dynamic JDK libraries, application I/O outside the explicit classpath, custom loader contents and transformed in-memory class bytes are not attested by the input record.

Tests run an external main, an unbound dependency JAR, an excluded bound class, resource objects, unchanged arguments, plain execution, all four metric states in one PID, and decoded function/lifetime telemetry. Negative controls cover stale bindings, ambiguous inputs, unsupported JARs and runtime reuse. This does not qualify every Java build tool or extend lifetime classloader support.
