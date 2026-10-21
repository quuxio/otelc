"""Keep injected helper classes separate from application-owned namespaces."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

PACKAGES = ("io.quux.otelc.methods.", "io.quux.otelc.lifecycle.", "io.quux.otelc.witness.probe.")
CLASS_SUFFIX = ".class"


def class_names(classpath: list[Path]) -> set[str]:
    """Inventory names without loading or initialising application classes."""
    names: set[str] = set()
    for entry in classpath:
        if entry.is_dir():
            names.update(path.relative_to(entry).as_posix().removesuffix(CLASS_SUFFIX).replace("/", ".")
                         for path in entry.rglob("*" + CLASS_SUFFIX))
        else:
            with zipfile.ZipFile(entry) as archive:
                names.update(name.removesuffix(CLASS_SUFFIX).replace("/", ".")
                             for name in archive.namelist() if name.endswith(CLASS_SUFFIX))
    return names


def tool(template: Path) -> Path:
    """Locate the standalone pinned ASM tool built with the extension."""
    return template.with_name(template.stem + "-helper-tool.jar")


def extension(classpath: list[Path], template: Path, runtime: Path) -> Path:
    """Generate a private extension only when an application owns a helper namespace."""
    from otelc import java

    names = class_names(classpath)
    original = template.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(original)) as archive:
            entries = archive.namelist()
    except zipfile.BadZipFile as error:
        raise ValueError("invalid Java extension archive") from error
    active = [package for package in PACKAGES
              if any(entry.startswith(package.replace(".", "/")) and entry.endswith(CLASS_SUFFIX) for entry in entries)]
    if not any(name.startswith(tuple(active)) for name in names):
        return template
    compiler = tool(template)
    if not compiler.is_file():
        raise ValueError("build the helper relocation tool with make java-build")
    template_sha = hashlib.sha256(original).hexdigest()
    tool_sha = hashlib.sha256(compiler.read_bytes()).hexdigest()
    names.update(entry.removesuffix(CLASS_SUFFIX).replace("/", ".") for entry in entries if entry.endswith(CLASS_SUFFIX))
    sequence = 0
    while True:
        prefix = f"io.quux.otelc.generated.h{template_sha[:16]}x{sequence}."
        if not any(name.startswith(prefix) for name in names):
            break
        sequence += 1
    packages = {package: prefix + package.split(".")[-2] + "." for package in active}
    plan = runtime / "helpers.properties"
    plan.write_text(java.properties(packages))
    plan.chmod(0o600)
    output = runtime / "helpers-extension.jar"
    subprocess.run([java.executable("java"), "-cp", str(compiler), "io.quux.otelc.tools.HelperRelocator",
                    str(template), str(output), str(plan)], capture_output=True, text=True, check=True, timeout=30)
    if (hashlib.sha256(template.read_bytes()).hexdigest() != template_sha
            or hashlib.sha256(compiler.read_bytes()).hexdigest() != tool_sha):
        raise ValueError("instrumentation tooling changed during helper relocation")
    output.chmod(0o600)
    mapping = {"schema_version": 1, "kind": "otelc.java-helper-map", "packages": packages,
               "template_sha256": template_sha, "tool_sha256": tool_sha,
               "generated_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
    descriptor = runtime / "helpers.json"
    descriptor.write_text(json.dumps(mapping, indent=2) + "\n")
    descriptor.chmod(0o600)
    return output
