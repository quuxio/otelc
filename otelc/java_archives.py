"""Inspect the selected JDK's effective archive bytes without loading application classes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from otelc import java, java_helpers

PROFILE = "java-runtime-archives-v1"
AMBIENT_OPTIONS = ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")


def tool() -> Path:
    """Locate the standalone archive resolver built with the pinned extension."""
    path = java_helpers.tool(java.ROOT / "adapters/java/target/java-extension-0.2.0-SNAPSHOT.jar")
    if not path.is_file():
        raise ValueError("build the runtime archive tool with make java-build first")
    return path


def require_fields(value: Any, fields: set[str]) -> None:
    """Keep the resolver protocol closed rather than accepting a partial inventory."""
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid runtime archive resolver fields")


def require_digest(value: Any) -> None:
    """Require a complete SHA-256 identity."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid runtime archive digest")


def class_path(name: str) -> str:
    """Convert a logical binary name to a safe private inspection path."""
    if (not isinstance(name, str) or not name or any(not part for part in name.split("."))
            or any(char in name for char in "/\\:") or any(ord(char) < 32 for char in name)):
        raise ValueError("invalid runtime archive class name")
    path = name.replace(".", "/") + ".class"
    if path == "module-info.class" or path.startswith("META-INF/"):
        raise ValueError("non-runtime archive metadata is not an application class")
    return path


def validate_members(archive: dict[str, Any], feature: int) -> None:
    """Retain real member identities, including when two members have identical bytes."""
    if not isinstance(archive["classes"], dict):
        raise ValueError("invalid runtime archive classes")
    for name, member in archive["classes"].items():
        require_fields(member, {"entry", "sha256"})
        require_digest(member["sha256"])
        expected = class_path(name)
        entry = member["entry"]
        if entry == expected:
            continue
        version = re.fullmatch(r"META-INF/versions/((?a:\d+))/" + re.escape(expected), entry) if isinstance(entry, str) else None
        if not version or not archive["multi_release"] or not 9 <= int(version[1]) <= feature:
            raise ValueError("invalid effective runtime archive member")


def validate_protocol(data: Any, indexed: list[tuple[int, Path]],
                      identities: dict[str, dict[str, Any]], feature: int) -> dict[str, Any]:
    """Bind every reported archive to its position and complete original file identity."""
    require_fields(data, {"schema_version", "jdk_feature", "archives"})
    if (type(data["schema_version"]) is not int or data["schema_version"] != 1
            or type(data["jdk_feature"]) is not int or data["jdk_feature"] != feature
            or not isinstance(data["archives"], list) or len(data["archives"]) != len(indexed)):
        raise ValueError("invalid runtime archive schema, JDK feature or archive count")
    archives = []
    for (index, path), archive in zip(indexed, data["archives"]):
        require_fields(archive, {"path", "sha256", "multi_release", "versioned_members", "classes"})
        require_digest(archive["sha256"])
        if (archive["path"] != str(path) or archive["sha256"] != identities[str(path)]["sha256"]
                or type(archive["multi_release"]) is not bool or type(archive["versioned_members"]) is not bool):
            raise ValueError("runtime archive identity or flags disagree")
        validate_members(archive, feature)
        archives.append({"classpath_index": index, **{key: value for key, value in archive.items()
                                                     if key != "versioned_members"}})
    return {"schema_version": 1, "profile": PROFILE, "jdk_feature": feature, "archives": archives}


def resolve(classpath: tuple[Path, ...]) -> dict[str, Any] | None:
    """Use one selected-JDK process to resolve all explicit JARs in classpath order."""
    from otelc.prepared_java import file_identity, unique_object

    indexed = [(index, path.resolve(strict=True)) for index, path in enumerate(classpath)
               if path.is_file() and path.suffix.lower() == ".jar"]
    if not indexed:
        return None
    if any(os.environ.get(name) for name in AMBIENT_OPTIONS):
        raise ValueError("runtime archive resolution requires unset ambient JVM option variables")
    compiler = tool()
    executable = Path(java.executable("java")).resolve(strict=True)
    tools = [file_identity(path) for path in (executable, compiler)]
    identities = {str(path): file_identity(path) for _, path in indexed}
    with tempfile.TemporaryDirectory(prefix="otelc-jars-") as temporary:
        output = Path(temporary) / "inventory.json"
        checked = subprocess.run([str(executable), "-cp", str(compiler), "io.quux.otelc.tools.JarInventory",
                                  "--output", str(output), *(str(path) for _, path in indexed)],
                                 capture_output=True, text=True, timeout=30)
        if checked.returncode:
            raise ValueError("invalid prepared JAR or unsupported runtime archive: " + checked.stderr.strip())
        data = json.loads(output.read_text(), object_pairs_hook=unique_object)
    version = subprocess.check_output([str(executable), "-version"], stderr=subprocess.STDOUT, text=True, timeout=30)
    matched = re.search(r'version "(\d+)', version)
    if not matched:
        raise ValueError("cannot identify the selected runtime archive JDK feature")
    result = validate_protocol(data, indexed, identities, int(matched[1]))
    if (any(file_identity(Path(identity["path"])) != identity for identity in tools)
            or any(file_identity(Path(path)) != identity for path, identity in identities.items())):
        raise ValueError("runtime archive inputs or tooling changed during resolution")
    return result


def copy_members(archive: dict[str, Any], output: Path) -> None:
    """Keep exact effective bytes and case-distinct names in a private ordinary JAR."""
    from otelc.prepared_java import file_identity

    path = Path(archive["path"])
    original = file_identity(path)
    if original["sha256"] != archive["sha256"]:
        raise ValueError("runtime archive changed before metadata inspection")
    try:
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(output, "x") as target:
            for name, member in archive["classes"].items():
                payload = source.read(member["entry"])
                if hashlib.sha256(payload).hexdigest() != member["sha256"]:
                    raise ValueError("effective runtime archive member digest changed")
                target.writestr(class_path(name), payload)
        with zipfile.ZipFile(output) as target:
            for name, member in archive["classes"].items():
                if hashlib.sha256(target.read(class_path(name))).hexdigest() != member["sha256"]:
                    raise ValueError("private runtime archive metadata copy changed")
    except (zipfile.BadZipFile, KeyError) as error:
        raise ValueError("invalid effective runtime archive member") from error
    if file_identity(path) != original:
        raise ValueError("runtime archive changed during metadata inspection")


@contextmanager
def metadata_classpath(classpath: tuple[Path, ...], resolution: dict[str, Any] | None,
                       runtime: Path) -> Iterator[list[Path]]:
    """Inspect exact selected bytes; never launch the application through these views."""
    if resolution is None:
        yield list(classpath)
        return
    with tempfile.TemporaryDirectory(prefix="metadata-", dir=runtime) as temporary:
        views = list(classpath)
        for archive in resolution["archives"]:
            view = Path(temporary) / f"{archive['classpath_index']}.jar"
            copy_members(archive, view)
            views[archive["classpath_index"]] = view
        yield views
