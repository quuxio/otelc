"""Generate official Java-agent configuration from the common policy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from otelc.config import Policy, Selection

AGENT_VERSION = "2.32.0"
AGENT_SHA256 = "f787eb6c7f3d18e69a431e108a15278d25ee37f83d68b678f621e063f3988f82"
PARSER_CLASS = ("io/opentelemetry/javaagent/shaded/io/opentelemetry/api/trace/"
                "propagation/internal/W3CTraceContextEncoding.class")
PARSER_SHA256 = "bb56f27410ee111a92ab5c172eacb136f1e69795d4e84906a036d7d3b1b42712"
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


def parser_patch() -> Path:
    """Require the exact official patched parser, with no extra bootstrap code."""
    path = ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT-bootstrap-patch.jar"
    if os.pathsep in str(path):
        raise ValueError("bootstrap parser patch path contains a classpath separator")
    if not path.is_file():
        raise ValueError("build the Java bootstrap parser patch with make java-build first")
    if path.stat().st_size > 262_144:
        raise ValueError("bootstrap parser patch JAR is too large")
    required = {PARSER_CLASS, "META-INF/LICENSE.opentelemetry", "META-INF/NOTICE.opentelemetry"}
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            files = {name for name in names if not name.endswith("/")}
            directories = {name for name in names if name.endswith("/")}
            if (len(set(names)) != len(names) or files != required
                    or any(not any(file.startswith(directory) for file in required) for directory in directories)):
                raise ValueError("unexpected or missing bootstrap patch entries")
            if any(info.orig_filename != info.filename or info.flag_bits & 1
                   or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                   for info in archive.infolist()):
                raise ValueError("unsupported bootstrap patch encryption or compression")
            if archive.getinfo(PARSER_CLASS).file_size > 16_384:
                raise ValueError("bootstrap parser class is too large")
            if hashlib.sha256(archive.read(PARSER_CLASS)).hexdigest() != PARSER_SHA256:
                raise ValueError("bootstrap parser patch checksum mismatch")
    except zipfile.BadZipFile as error:
        raise ValueError("invalid bootstrap parser patch JAR") from error
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


def selected_methods(name: str, output: str, selection: Selection) -> list[str]:
    """Reject partial overload selections that the upstream selector cannot express."""
    methods: dict[str, list[bool]] = {}
    for method, arguments in re.findall(r"\s([\w$]+)\(([^)]*)\)(?: throws [^;]+)?;", output):
        identity = f"{name}.{method}({arguments.replace(' ', '')})"
        methods.setdefault(method, []).append(selection.accepts(identity))
    selected = []
    for method, matches in methods.items():
        if any(matches) and not all(matches):
            raise ValueError("stock Java method selector cannot honour this overload exclusion")
        if all(matches):
            selected.append(method)
    return selected


def validate_lifetime_type(name: str, output: str) -> None:
    """Limit the initial profile to unambiguous successful construction boundaries."""
    if "final class " not in output:
        raise ValueError("initial Java lifetime profile requires a final concrete class")
    if len(re.findall(re.escape(name) + r"\(", output)) != 1:
        raise ValueError("initial Java lifetime profile requires one constructor")


def selected_return_types(output: str, methods: list[str]) -> set[str]:
    """Read reference return metadata needed by the pinned advice, without loading classes."""
    result = set()
    pattern = r"\s([\w$]+)\([^)]*\)(?: throws [^;]+)?;\s+descriptor: \([^)]*\)(\S+)"
    for method, descriptor in re.findall(pattern, output):
        returned = descriptor.lstrip("[")
        if method in methods and returned.startswith("L"):
            name = returned[1:-1].replace("/", ".")
            if not name or name.startswith(("-", "@")) or "\x00" in name:
                raise ValueError("invalid selected Java return type")
            result.add(name)
    return result


def has_release_method(output: str, methods: list[str]) -> bool:
    """Find a selected existing instance zero-argument void release method."""
    for release in methods:
        signature = r"(?m)^\s+(?![^\n]*\bstatic\b)[^\n]*\bvoid\s+" + re.escape(release) + r"\(\)(?: throws [^;]+)?;"
        if re.search(signature, output):
            return True
    return False


def resolve_bindings(classpath: list[Path], bindings: dict[str, str], policy: Policy
                     ) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """Apply common selectors only to classes with original-source bindings."""
    selected: dict[str, list[str]] = {}
    types: list[str] = []
    resources: list[str] = []
    return_types: set[str] = set()
    for name, source in sorted(bindings.items()):
        if not policy.selection("sources").accepts(source):
            continue
        output = subprocess.check_output([executable("javap"), "-p", "-s", "-classpath", os.pathsep.join(map(str, classpath)), name], text=True)
        methods = selected_methods(name, output, policy.selection("functions"))
        if methods:
            selected[name] = methods
            return_types.update(selected_return_types(output, methods))
        if policy.data["lifetimes"]["enabled"] and policy.selection("lifetimes").accepts(name):
            validate_lifetime_type(name, output)
            types.append(name)
            if has_release_method(output, policy.data["lifetimes"]["release_methods"]):
                resources.append(name)
    if return_types:
        checked = subprocess.run([executable("javap"), "-classpath", os.pathsep.join(map(str, classpath)),
                                  *sorted(return_types)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
        if checked.returncode:
            raise ValueError("selected Java return-type metadata is unavailable: " + checked.stderr.strip())
    return selected, types, resources


def command(policy: Policy, runtime: Path, *, plain: bool = False) -> list[str]:
    """Build the internal example and lower its common policy."""
    if "java" not in policy.data["languages"]:
        raise ValueError("java is not enabled by this policy")
    classes = build()
    bindings = {str(path.relative_to(classes).with_suffix("")).replace(os.sep, "."):
                "examples/java/BenchmarkApp.java" for path in classes.rglob("*.class")}
    return launch_command(policy, runtime, [classes], "examples.BenchmarkApp", bindings, plain=plain)


def context_configuration(policy: Policy) -> dict[str, str]:
    """Translate common propagation intent into the pinned official agent modules."""
    settings = policy.data["context"]
    values = {"otel.instrumentation.executors.enabled": str(settings["tasks"]).lower(),
              "otel.instrumentation.java-http-client.enabled": str(settings["http"]).lower(),
              "otel.instrumentation.java-http-server.enabled": str(settings["http"]).lower(),
              "otel.instrumentation.otelc-http-headers.enabled": str(settings["http"]).lower()}
    if settings["http"]:
        values["otel.propagators"] = "tracecontext,baggage"
    return values


def validate_http_header_shape() -> None:
    """Refuse an incompatible JDK before enabling the header-view preservation guard."""
    declarations = {
        "jdk.internal.net.http.ImmutableHttpRequest": ("private final java.net.http.HttpHeaders headers;",
                                                      "public java.net.http.HttpHeaders headers();"),
        "jdk.internal.net.http.HttpRequestImpl": ("private final java.net.http.HttpHeaders userHeaders;",
                                                  "public java.net.http.HttpHeaders headers();",
                                                  "java.net.http.HttpHeaders getUserHeaders();"),
    }
    for owner, expected in declarations.items():
        validate_http_type(owner, expected)


def validate_http_type(owner: str, expected: tuple[str, ...]) -> None:
    """Inspect JDK declarations without executing application methods or loading their classes."""
    checked = subprocess.run([executable("javap"), "-p", "--module", "java.net.http",
                              owner],
                             capture_output=True, text=True, timeout=30)
    if checked.returncode or any(declaration not in checked.stdout for declaration in expected):
        raise ValueError("HTTP propagation requires the qualified JDK immutable-request header layout")


def launch_command(policy: Policy, runtime: Path, classpath: list[Path], main_class: str,
                   bindings: dict[str, str], *, plain: bool = False,
                   arguments: tuple[str, ...] = ()) -> list[str]:
    """Translate one policy without compiling or changing application inputs."""
    if "java" not in policy.data["languages"]:
        raise ValueError("java is not enabled by this policy")
    launch = [executable("java")]
    application = ["-cp", os.pathsep.join(map(str, classpath)), main_class, *arguments]
    if plain:
        return launch + application
    extension = ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar"
    if not extension.is_file():
        raise ValueError("build the Java extension with make java-build first")
    from otelc import java_helpers

    extension = java_helpers.extension(classpath, extension, runtime)
    selected, types, resources = resolve_bindings(classpath, bindings, policy)
    selection = json.dumps({"methods": selected, "types": types, "resources": resources}, sort_keys=True)
    (runtime / "selection.json").write_text(selection + "\n")
    data = policy.data
    if data["context"]["http"]:
        validate_http_header_shape()
    values = {
        "otel.javaagent.enabled": "true",
        "otel.sdk.disabled": "false",
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
        "otel.instrumentation.methods.enabled": "false",
        "otel.instrumentation.otelc-methods.enabled": "true",
        "otel.instrumentation.otelc-lifetimes.enabled": "true",
        "otel.instrumentation.opentelemetry-api.enabled": "true",
        "otel.instrumentation.methods.include": ";".join(f"{name}[{','.join(methods)}]" for name, methods in sorted(selected.items())),
    }
    values.update(context_configuration(policy))
    configuration = runtime / "agent.properties"
    configuration.write_text(properties(values))
    plan = runtime / "plan.properties"
    plan.write_text(properties({
        "control.socket": str(runtime / "control.sock"),
        "metrics.collection": str(data["metrics"]["collection"]).lower(),
        "metrics.emission": str(data["metrics"]["emission"]).lower(),
        "metrics.buckets": ",".join(map(str, data["metrics"]["histogram_boundaries_seconds"])),
        "health.report": str(runtime / "health.json"),
        "health.policy-sha256": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
        "health.selection-sha256": hashlib.sha256(selection.encode()).hexdigest(),
        "health.shutdown-timeout-ms": str(data["export"]["shutdown_timeout_ms"]),
    }))
    launch += [f"-Xbootclasspath/a:{parser_patch()}",
               f"-javaagent:{agent()}", f"-Dotel.javaagent.extensions={extension}",
               f"-Dotel.javaagent.configuration-file={configuration}", f"-Dotelc.plan={plan}",
               f"-Dotelc.traces.enabled={str(data['traces']['enabled']).lower()}",
               f"-Dotelc.lifetime.types={','.join(types)}",
               f"-Dotelc.lifetime.resources={','.join(resources)}",
               f"-Dotelc.lifetime.max-live={data['lifetimes']['max_live']}",
               f"-Dotelc.lifetime.release={','.join(data['lifetimes']['release_methods'])}"]
    # The common policy owns these settings; ambient agent variables cannot
    # replace the chosen functions or remove the pipeline needed for live enable.
    launch.extend(f"-D{key}={value}" for key, value in sorted(values.items()))
    return launch + application
