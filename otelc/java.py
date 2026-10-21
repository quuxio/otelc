"""Generate official Java-agent configuration from the common policy."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from otelc.config import Policy

AGENT_VERSION = "2.32.0"
AGENT_SHA256 = "f787eb6c7f3d18e69a431e108a15278d25ee37f83d68b678f621e063f3988f82"
ROOT = Path(__file__).resolve().parent.parent


def executable(name: str) -> str:
    """Use an explicitly selected JDK, or the installed development JDK."""
    choices = [Path(os.environ.get("JAVA_HOME", "/missing")) / "bin" / name,
               Path("/opt/homebrew/opt/openjdk@21/bin") / name]
    return next((str(path) for path in choices if path.is_file()), shutil.which(name) or name)


def agent() -> Path:
    """Fetch and verify the exact official agent before it is executed."""
    path = ROOT / "build/tools" / f"opentelemetry-javaagent-{AGENT_VERSION}.jar"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".download")
        url = f"https://repo.maven.apache.org/maven2/io/opentelemetry/javaagent/opentelemetry-javaagent/{AGENT_VERSION}/{path.name}"
        subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "60", url, "--output", str(temporary)], check=True)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != AGENT_SHA256:
            temporary.unlink()
            raise ValueError("official Java agent checksum mismatch")
        temporary.replace(path)
    if hashlib.sha256(path.read_bytes()).hexdigest() != AGENT_SHA256:
        raise ValueError("cached official Java agent checksum mismatch")
    return path


def build() -> Path:
    """Compile the ordinary example without adding application dependencies."""
    directory = ROOT / "build/examples/java"
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run([executable("javac"), "-d", str(directory), str(ROOT / "examples/java/BenchmarkApp.java")], check=True)
    return directory


def properties(values: dict[str, str]) -> str:
    """Write deterministic Java properties, escaping syntax and non-ASCII text."""
    def escape(value: str) -> str:
        result = ""
        for char in value:
            if char in "\\:=#! " or char in "\n\r\t":
                result += {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, "\\" + char)
            elif ord(char) > 126 or ord(char) < 32:
                result += "".join(f"\\u{int.from_bytes(unit, 'big'):04x}" for unit in (char.encode("utf-16-be")[i:i + 2] for i in range(0, len(char.encode("utf-16-be")), 2)))
            else:
                result += char
        return result

    return "".join(f"{escape(key)}={escape(value)}\n" for key, value in sorted(values.items()))


def command(policy: Policy, runtime: Path, *, plain: bool = False) -> list[str]:
    """Lower one policy into the official agent and extension launch arguments."""
    if "java" not in policy.data["languages"]:
        raise ValueError("java is not enabled by this policy")
    classes = build()
    launch = [executable("java")]
    if plain:
        return launch + ["-cp", str(classes), "examples.BenchmarkApp"]
    extension = ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar"
    if not extension.is_file():
        raise ValueError("build the Java extension with make java-build first")
    source = "examples/java/BenchmarkApp.java"
    selected: dict[str, list[str]] = {}
    types = []
    resources = []
    if policy.selection("sources").accepts(source):
        for path in sorted(classes.rglob("*.class")):
            name = str(path.relative_to(classes).with_suffix("")).replace(os.sep, ".")
            output = subprocess.check_output([executable("javap"), "-p", "-classpath", str(classes), name], text=True)
            methods: dict[str, list[bool]] = {}
            for method, arguments in re.findall(r"\s([\w$]+)\(([^)]*)\)(?: throws [^;]+)?;", output):
                identity = f"{name}.{method}({arguments.replace(' ', '')})"
                methods.setdefault(method, []).append(policy.selection("functions").accepts(identity))
            for method, matches in methods.items():
                if any(matches) and not all(matches):
                    raise ValueError("stock Java method selector cannot honour this overload exclusion")
                if all(matches):
                    selected.setdefault(name, []).append(method)
            if policy.data["lifetimes"]["enabled"] and policy.selection("lifetimes").accepts(name):
                if "final class " not in output:
                    raise ValueError("initial Java lifetime profile requires a final concrete class")
                if len(re.findall(re.escape(name) + r"\(", output)) != 1:
                    raise ValueError("initial Java lifetime profile requires one constructor")
                types.append(name)
                for release in policy.data["lifetimes"]["release_methods"]:
                    signature = r"(?m)^\s+(?![^\n]*\bstatic\b)[^\n]*\bvoid\s+" + re.escape(release) + r"\(\)(?: throws [^;]+)?;"
                    if re.search(signature, output):
                        resources.append(name)
                        break
    data = policy.data
    values = {
        "otel.service.name": data["resource"]["service_name"],
        "otel.resource.attributes": "otelc.language=java",
        "otel.exporter.otlp.endpoint": data["export"]["endpoint"],
        "otel.exporter.otlp.protocol": "http/protobuf",
        "otel.exporter.otlp.timeout": str(data["export"]["timeout_ms"]),
        "otel.metric.export.interval": str(data["export"]["interval_ms"]),
        "otel.metrics.exporter": "otlp",
        "otel.logs.exporter": "none",
        "otel.traces.exporter": "otlp" if data["traces"]["enabled"] else "none",
        "otel.traces.sampler": "parentbased_traceidratio" if data["traces"]["enabled"] else "always_off",
        "otel.traces.sampler.arg": str(data["traces"]["sample_ratio"]),
        "otel.instrumentation.common.default-enabled": "false",
        "otel.instrumentation.methods.enabled": "true",
        "otel.instrumentation.otelc-lifetimes.enabled": "true",
        "otel.instrumentation.opentelemetry-api.enabled": "true",
        "otel.instrumentation.methods.include": ";".join(f"{name}[{','.join(methods)}]" for name, methods in sorted(selected.items())),
    }
    configuration = runtime / "agent.properties"
    configuration.write_text(properties(values))
    plan = runtime / "plan.properties"
    plan.write_text(properties({
        "control.socket": str(runtime / "control.sock"),
        "metrics.collection": str(data["metrics"]["collection"]).lower(),
        "metrics.emission": str(data["metrics"]["emission"]).lower(),
        "metrics.buckets": ",".join(map(str, data["metrics"]["histogram_boundaries_seconds"])),
    }))
    launch += [f"-javaagent:{agent()}", f"-Dotel.javaagent.extensions={extension}",
               f"-Dotel.javaagent.configuration-file={configuration}", f"-Dotelc.plan={plan}",
               f"-Dotelc.traces.enabled={str(data['traces']['enabled']).lower()}",
               f"-Dotelc.lifetime.types={','.join(types)}",
               f"-Dotelc.lifetime.resources={','.join(resources)}",
               f"-Dotelc.lifetime.max-live={data['lifetimes']['max_live']}",
               f"-Dotelc.lifetime.release={','.join(data['lifetimes']['release_methods'])}"]
    # The common policy owns these settings; ambient agent variables cannot
    # replace the chosen functions or remove the pipeline needed for live enable.
    launch.extend(f"-D{key}={value}" for key, value in sorted(values.items()))
    return launch + ["-cp", str(classes), "examples.BenchmarkApp"]
