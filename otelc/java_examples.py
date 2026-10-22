"""Build the ordinary context examples and attest their original source bindings."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from otelc import java
from otelc.prepared_java import PreparedApplication, file_identity


def build(output: Path) -> dict[str, Path]:
    """Compile into fresh directories so stale nested classes cannot enter a manifest."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = "TaskApp"
    source = java.ROOT / "examples/java" / (name + ".java")
    original = file_identity(source)
    classes = Path(tempfile.mkdtemp(prefix=name + "-", dir=output))
    subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
    bindings = {path.stem: {"source": str(source),
                "policy_path": "examples/java/" + source.name,
                "source_sha256": original["sha256"],
                "class_sha256": file_identity(path)["sha256"],
                "provenance": "javac from the attested original example source"}
                for path in sorted(classes.glob("*.class"))}
    manifest = output / (name + ".json")
    manifest.write_text(json.dumps({"schema_version": 1, "main_class": name,
        "classpath": [classes.name], "bindings": bindings}, indent=2) + "\n")
    PreparedApplication.load(manifest)
    return {name: manifest}


if __name__ == "__main__":
    print(json.dumps({name: str(path) for name, path in
        build(java.ROOT / "build/examples/context/java").items()}, indent=2))
