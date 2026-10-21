"""Launch prepared Java inputs with explicit, digest-bound source provenance."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from otelc import java
from otelc.config import Policy

AMBIENT_OPTIONS = ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")


def file_identity(path: Path) -> dict[str, Any]:
    """Record content and filesystem identity; refuse a changing read."""
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"input is not a regular file: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    stable = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    # Reading may update atime; that is not a mutation of the declared input.
    if stable(before) != stable(after) or not stat.S_ISREG(after.st_mode):
        raise ValueError(f"input changed or is not a regular file: {path}")
    return {"path": str(path), "sha256": digest, "size": after.st_size,
            "inode": after.st_ino, "device": after.st_dev,
            "mtime_ns": after.st_mtime_ns}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse duplicate JSON keys instead of silently replacing a binding."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def required_text(value: Any) -> str:
    """Require a nonempty, printable manifest value."""
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("manifest strings must be nonempty and printable")
    return value


def binary_name(value: Any) -> str:
    """Keep binary identities distinct from JVM options and selector syntax."""
    name = required_text(value)
    if not re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", name) or name.startswith("java."):
        raise ValueError("expected an application binary class name with ASCII segment initials")
    return name


def jar_classes(path: Path) -> dict[str, str]:
    """Inventory class bytes without extracting or following implicit classpaths."""
    try:
        return read_jar_classes(path)
    except (zipfile.BadZipFile, UnicodeDecodeError) as error:
        raise ValueError(f"invalid prepared JAR: {path}") from error


def read_jar_classes(path: Path) -> dict[str, str]:
    """Read an explicit, single-version JAR inventory."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or any(name.startswith("META-INF/versions/") for name in names):
            raise ValueError("duplicate or multi-release JAR entries are unsupported")
        if "META-INF/INDEX.LIST" in names:
            raise ValueError("implicit JAR indexes are unsupported")
        if "META-INF/MANIFEST.MF" in names:
            manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8")
            if any(line.lower().startswith("class-path:") for line in manifest.splitlines()):
                raise ValueError("implicit JAR Class-Path is unsupported; supply the complete classpath")
        return {name[:-6].replace("/", "."): hashlib.sha256(archive.read(name)).hexdigest()
                for name in names if name.endswith(".class") and name != "module-info.class"}


def directory_classes(entry: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Inventory class bytes and resources without following symlinked inputs."""
    classes = {}
    inputs = []
    for path in sorted(entry.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks inside a prepared classpath are unsupported")
        if not path.is_file():
            continue
        identity = file_identity(path)
        inputs.append(identity)
        if path.suffix == ".class" and path.name != "module-info.class":
            name = path.relative_to(entry).as_posix()[:-6].replace("/", ".")
            classes[name] = identity["sha256"]
    return classes, inputs


def inspect_classpath(classpath: tuple[Path, ...]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Bind every explicit classpath file and refuse ambiguous class resolution."""
    classes: dict[str, str] = {}
    inputs = []
    for entry in classpath:
        if entry.is_dir():
            current, files = directory_classes(entry)
            inputs.extend(files)
        elif entry.is_file() and entry.suffix.lower() == ".jar":
            inputs.append(file_identity(entry))
            current = jar_classes(entry)
        else:
            raise ValueError("classpath entries must be existing directories or JARs")
        if classes.keys() & current.keys():
            raise ValueError("duplicate class definitions in the prepared classpath")
        classes.update(current)
    return classes, inputs


@dataclass(frozen=True)
class PreparedApplication:
    """Caller-attested compiler bindings, checked against unchanged input bytes."""

    manifest: Path
    main_class: str
    classpath: tuple[Path, ...]
    bindings: dict[str, dict[str, str]]
    manifest_identity: dict[str, Any]

    @classmethod
    def load(cls, manifest: Path) -> PreparedApplication:
        """Validate the explicit manifest before any JVM or build can run."""
        manifest = manifest.resolve(strict=True)
        manifest_identity = file_identity(manifest)
        data = json.loads(manifest.read_text(), object_pairs_hook=unique_object)
        if (not isinstance(data, dict) or set(data) != {"schema_version", "main_class", "classpath", "bindings"}
                or type(data["schema_version"]) is not int or data["schema_version"] != 1):
            raise ValueError("expected prepared Java manifest schema_version=1 with known fields")
        paths = data["classpath"]
        bindings = data["bindings"]
        if not isinstance(paths, list) or not paths or not isinstance(bindings, dict):
            raise ValueError("prepared Java requires a nonempty classpath and source bindings")
        classpath = tuple((manifest.parent / required_text(path)).resolve(strict=True) for path in paths)
        if any(os.pathsep in str(path) or "*" in str(path) for path in classpath):
            raise ValueError("classpath separators and wildcards are unsupported in entry paths")
        if len(set(classpath)) != len(classpath):
            raise ValueError("duplicate classpath entry")
        application = cls(manifest, binary_name(data["main_class"]), classpath, bindings, manifest_identity)
        application.input_identity()
        return application

    def input_identity(self) -> dict[str, Any]:
        """Recheck all declared bindings and capture the complete explicit inputs."""
        if any(os.environ.get(name) for name in AMBIENT_OPTIONS):
            raise ValueError("prepared Java requires unset ambient JVM option variables")
        if file_identity(self.manifest) != self.manifest_identity:
            raise ValueError("prepared manifest changed after loading")
        classes, inputs = inspect_classpath(self.classpath)
        sources = {}
        for name, binding in self.bindings.items():
            source = self.validate_binding(name, binding, classes)
            sources[str(source)] = file_identity(source)
            if sources[str(source)]["sha256"] != binding["source_sha256"]:
                raise ValueError(f"stale source binding: {name}")
        if self.main_class not in self.bindings:
            raise ValueError("main class requires an original-source binding")
        return {"manifest": self.manifest_identity, "classpath": inputs,
                "main_class": self.main_class, "ordered_classpath": list(map(str, self.classpath)),
                "sources": list(sources.values()), "bindings": self.bindings}

    def validate_binding(self, name: str, binding: Any, classes: dict[str, str]) -> Path:
        """Require both byte digests and an explicit original-path attestation."""
        fields = {"source", "policy_path", "source_sha256", "class_sha256", "provenance"}
        binary_name(name)
        if not isinstance(binding, dict) or set(binding) != fields:
            raise ValueError(f"incomplete source binding: {name}")
        for value in binding.values():
            required_text(value)
        policy_path = PurePosixPath(binding["policy_path"])
        if policy_path.is_absolute() or ".." in policy_path.parts or str(policy_path) == ".":
            raise ValueError("policy_path must identify an original relative source path")
        if name not in classes or classes[name] != binding["class_sha256"]:
            raise ValueError(f"missing or stale class binding: {name}")
        return (self.manifest.parent / binding["source"]).resolve(strict=True)


@dataclass(frozen=True)
class PreparedLaunch:
    """Resolved JVM arguments and evidence for one caller-managed process."""

    application: PreparedApplication
    argv: tuple[str, ...]
    identity: dict[str, Any]

    def verify_inputs(self) -> None:
        """Refuse stale inputs before launch or a changed result after execution."""
        if self.application.input_identity() != self.identity["application"]:
            raise ValueError("prepared application inputs changed")
        for expected in self.identity["tooling"]:
            if file_identity(Path(expected["path"])) != expected:
                raise ValueError("prepared launch tooling or configuration changed")


def prepare(application: PreparedApplication, policy: Policy, runtime: Path, *,
            plain: bool = False, arguments: tuple[str, ...] = (),
            diagnostic_profile: bool = False) -> PreparedLaunch:
    """Create a launch plan in a fresh private directory, without compiling."""
    runtime = runtime.resolve(strict=True)
    metadata = runtime.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700 or any(runtime.iterdir()):
        raise ValueError("prepared runtime must be a fresh, owned 0700 directory")
    if any(not isinstance(argument, str) for argument in arguments):
        raise ValueError("application arguments must be strings")
    log_path = str(runtime / "class-load.log")
    if (any(character in log_path for character in ":,=%") or any(ord(char) < 32 for char in log_path)
            or len(os.fsencode(runtime / "control.sock")) > 100):
        raise ValueError("use a short runtime path without JVM log delimiters (:,=)")
    application_identity = copy.deepcopy(application.input_identity())
    bindings = {name: binding["policy_path"] for name, binding in application.bindings.items()}
    argv = java.launch_command(policy, runtime, list(application.classpath),
                               application.main_class, bindings, plain=plain, arguments=arguments)
    executable = Path(java.executable("java")).resolve(strict=True)
    # JVM origin diagnostics go to a separate file in both plain and instrumented runs.
    argv[0] = str(executable)
    argv.insert(1, f"-Xlog:class+load=info:file={log_path}")
    if diagnostic_profile:
        argv.insert(1, "-Xshare:off")
    agent_log = None if plain else str(runtime / "agent.log")
    if agent_log:
        # This is the pinned agent's isolated, shaded SLF4J backend, not an
        # application logger and not a redirection/filter of System.err.
        argv[1:1] = ["-Dotel.javaagent.logging=simple",
                     f"-Dio.opentelemetry.javaagent.slf4j.simpleLogger.logFile={agent_log}"]
    tooling = [file_identity(executable)]
    if not plain:
        tooling.extend(file_identity(path) for path in (
            java.agent(), java.ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar",
            runtime / "agent.properties", runtime / "plan.properties", runtime / "selection.json"))
    identity = {"schema_version": 1, "language": "java", "plain": plain,
                "jvm_profile": "diagnostic-no-cds" if diagnostic_profile else "default",
                "agent_diagnostics": agent_log,
                "health_report": None if plain else str(runtime / "health.json"),
                "selection_sha256": None if plain else hashlib.sha256(
                    (runtime / "selection.json").read_bytes().rstrip(b"\n")).hexdigest(),
                "argv": argv, "main_class": application.main_class,
                "ordered_classpath": list(map(str, application.classpath)),
                "application": application_identity, "tooling": tooling,
                "java_version": subprocess.check_output([str(executable), "-version"], stderr=subprocess.STDOUT, text=True),
                "policy_sha256": hashlib.sha256(json.dumps(policy.data, sort_keys=True).encode()).hexdigest(),
                "loaded_origins": log_path, "control_socket": None if plain else str(runtime / "control.sock")}
    launch = PreparedLaunch(application, tuple(argv), identity)
    launch.verify_inputs()
    with (runtime / "launch.json").open("x") as output:
        output.write(json.dumps(identity, indent=2) + "\n")
    return launch


def run(manifest: Path, policy: Policy, runtime: Path, *, plain: bool = False,
        prepare_only: bool = False, arguments: tuple[str, ...] = (),
        diagnostic_profile: bool = False) -> int:
    """Run the public CLI profile, retaining PID and post-run input verification."""
    application = PreparedApplication.load(manifest)
    runtime.mkdir(mode=0o700)
    launch = prepare(application, policy, runtime, plain=plain, arguments=arguments,
                     diagnostic_profile=diagnostic_profile)
    if prepare_only:
        print(json.dumps(launch.identity))
        return 0
    with subprocess.Popen(launch.argv) as process:
        (runtime / "process.json").write_text(json.dumps({"pid": process.pid}) + "\n")
        result = process.wait()
    launch.verify_inputs()
    return result
