"""JAR member names remain distinct on case-insensitive host filesystems."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from otelc import java, prepared_java
from otelc.config import Policy
from test_java_integration import Capture
from test_prepared_java import binding


class ArchiveCaseTests(unittest.TestCase):
    def test_case_distinct_classes_preserve_selection_and_received_probes(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary).resolve()
            bindings, members = {}, {}
            # Separate build directories preserve both inputs on a case-insensitive host.
            for name, method, result in (("Foo", "upper", 1), ("foo", "lower", 2)):
                directory = root / method
                directory.mkdir()
                source = directory / (name + ".java")
                source.write_text(f"public class {name} {{ public static int {method}() {{ return {result}; }} }}")
                classes = directory / "classes"
                subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
                compiled = classes / (name + ".class")
                bindings[name] = binding(source, compiled, f"src/{name}.java")
                members[name + ".class"] = compiled
            archive_path = root / "application.jar"
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, path in members.items():
                    archive.write(path, name)
            source = root / "Main.java"
            source.write_text("public class Main { public static void main(String[] args) { System.out.println(Foo.upper() + foo.lower()); }}")
            classes = root / "classes"
            subprocess.run([java.executable("javac"), "-cp", str(archive_path), "-d", str(classes), str(source)], check=True)
            bindings["Main"] = binding(source, classes / "Main.class", "src/Main.java")
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Main",
                "classpath": [str(classes), str(archive_path)], "bindings": bindings}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "sources": {"include": ["src/**"]},
                "functions": {"include": ["Foo.upper()", "foo.lower()"]},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain, diagnostic_profile=True)
                if not plain:
                    selection = json.loads((runtime / "selection.json").read_text())
                    self.assertEqual(selection["methods"], {"Foo": ["upper"], "foo": ["lower"]})
                result = subprocess.run(launch.argv, capture_output=True, text=True, check=True, timeout=15)
                self.assertEqual((result.stdout, result.stderr), ("3\n", ""))
                launch.verify_inputs()
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            self.assertEqual(sorted(span.name for span in spans), ["Foo.upper", "foo.lower"])
            functions = {next(item.value.string_value for item in point.attributes if item.key == "function")
                         for point in capture.points("otelc.function.calls")}
            self.assertEqual(functions, {"upper", "lower"})
