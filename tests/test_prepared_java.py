"""Prepared inputs refuse drift and launch real external classes unchanged."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from otelc import control, java, prepared_java
from otelc.config import Policy, Selection
from test_java_integration import Capture, batch


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def binding(source: Path, compiled: Path, policy_path: str) -> dict[str, str]:
    return {"source": str(source), "policy_path": policy_path,
            "source_sha256": digest(source), "class_sha256": digest(compiled),
            "provenance": "test fixture javac invocation; original source and class digests"}


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.classes = self.root / "classes"
        self.classes.mkdir()
        self.source = self.root / "Main.java"
        self.source.write_text("class Main {}")
        self.compiled = self.classes / "Main.class"
        self.compiled.write_bytes(b"fixture class bytes")
        self.path = self.root / "application.json"
        self.data = {"schema_version": 1, "main_class": "Main", "classpath": ["classes"],
                     "bindings": {"Main": binding(self.source, self.compiled, "src/Main.java")}}
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data))

    def test_rejects_invalid_and_stale_bindings(self) -> None:
        cases = [{"schema_version": True}, {"extra": 1}, {"classpath": []},
                 {"classpath": ["classes", "classes"]}, {"classpath": ["Main.java"]},
                 {"main_class": "Other"}, {"main_class": ""}, {"bindings": []},
                 {"main_class": "-version"}, {"main_class": "Main;Other"}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.path.write_text(json.dumps(self.data | changes))
                with self.assertRaises(ValueError):
                    prepared_java.PreparedApplication.load(self.path)
        for changes in ({"source_sha256": "stale"}, {"class_sha256": "stale"},
                        {"policy_path": "../Main.java"}, {"policy_path": "/Main.java"},
                        {"policy_path": "."}, {"provenance": ""}, {"source": 1}):
            candidate = copy.deepcopy(self.data)
            candidate["bindings"]["Main"].update(changes)
            self.path.write_text(json.dumps(candidate))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                prepared_java.PreparedApplication.load(self.path)
        self.data["bindings"]["Main"].pop("provenance")
        self.save()
        with self.assertRaisesRegex(ValueError, "incomplete source binding"):
            prepared_java.PreparedApplication.load(self.path)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            prepared_java.PreparedApplication.load(self.path)

    def test_rejects_ambiguous_jars_and_untracked_inputs(self) -> None:
        jar = self.root / "library.jar"
        for name, payload in (("META-INF/MANIFEST.MF", b"Class-Path: hidden.jar\n"),
                              ("META-INF/INDEX.LIST", b"JarIndex-Version: 1.0\n"),
                              ("META-INF/versions/21/Main.class", b"versioned"),
                              ("Main.class", b"duplicate")):
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr(name, payload)
            self.data["classpath"] = ["classes", "library.jar"]
            self.save()
            with self.subTest(name=name), self.assertRaises(ValueError):
                prepared_java.PreparedApplication.load(self.path)
        self.data["classpath"] = ["classes"]
        self.save()
        (self.classes / "linked").symlink_to(self.source)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            prepared_java.PreparedApplication.load(self.path)
        jar.write_bytes(b"invalid JAR")
        with self.assertRaisesRegex(ValueError, "invalid prepared JAR"):
            prepared_java.jar_classes(jar)
        bad_path = self.root / "classes:other"
        bad_path.mkdir()
        self.data["classpath"] = [bad_path.name]
        self.save()
        with self.assertRaisesRegex(ValueError, "separators and wildcards"):
            prepared_java.PreparedApplication.load(self.path)

    def test_launch_identity_refuses_changes_and_reuse(self) -> None:
        application = prepared_java.PreparedApplication.load(self.path)
        policy = Policy.parse({"schema_version": 3})
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        launch = prepared_java.prepare(application, policy, runtime, plain=True)
        launch.verify_inputs()
        self.assertIsNone(launch.identity["control_socket"])
        self.assertTrue((runtime / "launch.json").is_file())
        with self.assertRaisesRegex(ValueError, "fresh"):
            prepared_java.prepare(application, policy, runtime, plain=True)
        self.source.write_text("class Main { int changed; }")
        with self.assertRaisesRegex(ValueError, "stale source"):
            launch.verify_inputs()
        self.source.write_text("class Main {}")
        self.save()
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            application.input_identity()

    def test_read_access_time_does_not_invalidate_an_unchanged_input(self) -> None:
        os.utime(self.source, ns=(1, self.source.stat().st_mtime_ns))
        self.assertEqual(prepared_java.file_identity(self.source)["sha256"], digest(self.source))

    def test_launch_refuses_nonprivate_runtime_and_ambient_options(self) -> None:
        application = prepared_java.PreparedApplication.load(self.path)
        policy = Policy.parse({"schema_version": 3})
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            prepared_java.prepare(application, policy, runtime, plain=True)
        runtime.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "arguments must be strings"):
            prepared_java.prepare(application, policy, runtime, arguments=(1,))
        with patch.dict(os.environ, {"JAVA_TOOL_OPTIONS": "-Dunexpected=true"}):
            with self.assertRaisesRegex(ValueError, "ambient JVM"):
                application.input_identity()

    def test_partial_overload_selection_is_refused(self) -> None:
        output = "  void workload(int);\n  void workload(java.lang.String);\n"
        selection = Selection(("external.Driver.workload(int)",), ())
        with self.assertRaisesRegex(ValueError, "overload exclusion"):
            java.selected_methods("external.Driver", output, selection)


class PreparedIntegrationTests(unittest.TestCase):
    def test_external_main_jar_dependencies_source_selection_and_live_states(self) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(prefix="otelc-prep-", dir="/tmp") as directory:
            root = Path(directory)
            sources = root / "src"
            classes = root / "classes"
            dependencies = root / "dependencies"
            sources.mkdir()
            classes.mkdir()
            dependencies.mkdir()
            dependency = sources / "Maths.java"
            dependency.write_text("package dependency; public final class Maths { public static int workload(int n) { return n+1; } }")
            subprocess.run([java.executable("javac"), "-d", str(dependencies), str(dependency)], check=True)
            jar = root / "dependency.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.write(dependencies / "dependency/Maths.class", "dependency/Maths.class")
            excluded = sources / "Excluded.java"
            excluded.write_text("package external; final class Excluded { static void workload() {} }")
            source = sources / "Driver.java"
            source.write_text('''package external;
import java.io.BufferedReader;
import java.io.InputStreamReader;
public final class Driver {
  static final class WorkItem { WorkItem() {} void close() {} }
  static int workload(int n) {
    WorkItem item = new WorkItem();
    int value = dependency.Maths.workload(n);
    Excluded.workload(); item.close(); return value;
  }
  public static void main(String[] args) throws Exception {
    var input = new BufferedReader(new InputStreamReader(System.in));
    String line;
    while ((line = input.readLine()) != null && !line.equals("quit")) {
      int count = Integer.parseInt(line); long result = 0;
      for (int n=0; n<count; n++) result += workload(n);
      System.out.println("{\\"checksum\\":"+result+",\\"calls\\":"+count+",\\"argument\\":\\""+args[0]+"\\"}");
    }
  }
}''')
            subprocess.run([java.executable("javac"), "-cp", str(jar), "-d", str(classes), str(source), str(excluded)], check=True)
            bindings = {}
            for path in classes.rglob("*.class"):
                name = path.relative_to(classes).as_posix()[:-6].replace("/", ".")
                original = excluded if name == "external.Excluded" else source
                bindings[name] = binding(original, path, "src/" + original.name)
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "external.Driver",
                                            "classpath": ["classes", "dependency.jar"], "bindings": bindings}))
            application = prepared_java.PreparedApplication.load(manifest)
            data = Policy.parse({"schema_version": 3}).data
            data["sources"] = {"include": ["src/**"], "exclude": ["src/Excluded.java"]}
            data["functions"]["include"] = ["external.Driver.workload(int)",
                                               "external.Excluded.workload()", "dependency.Maths.workload(int)"]
            data["lifetimes"]["include"] = ["*WorkItem"]
            data["lifetimes"]["enabled"] = True
            data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
            data["metrics"].update(collection=False, emission=False)
            data["traces"]["sample_ratio"] = 1.
            data["traces"]["enabled"] = True
            policy = Policy.parse(data)
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                launch = prepared_java.prepare(application, policy, runtime, plain=plain, arguments=("argument with spaces",))
                with (runtime / "stderr").open("w+") as errors:
                    process = subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True)
                    try:
                        if not plain:
                            first = control.wait_ready(runtime / "control.sock", process.pid)
                            self.assertFalse(first["collection"])
                        states = ((False, False), (True, True), (True, False),
                                  (False, True), (False, False), (True, True))
                        for index, (collection, emission) in enumerate(states):
                            if not plain:
                                control.request(runtime / "control.sock", "collection " + ("on" if collection else "off"))
                                current = control.request(runtime / "control.sock", "emission " + ("on" if emission else "off"))
                                self.assertEqual(current["pid"], process.pid)
                            self.assertEqual(batch(process, 2), {"checksum": 3, "calls": 2, "argument": "argument with spaces"})
                            if not plain:
                                health = control.request(runtime / "control.sock", "flush")
                                self.assertEqual(health["completed"], (0, 2, 4, 4, 4, 6)[index])
                                # Disabled SDK meters do not collect/export their retained values.
                                # Re-enabling both later must recover collection-only observations.
                                self.assertEqual(capture.calls(), (0, 2, 2, 2, 2, 6)[index])
                        if not plain:
                            control.request(runtime / "control.sock", "flush")
                            self.assertEqual(capture.calls(), 6)
                            self.assertEqual(capture.lifetime_count("otelc.lifetime.completed", "resource", "external.Driver$WorkItem"), 6)
                        process.stdin.write("quit\n")
                        process.stdin.flush()
                        self.assertEqual(process.wait(timeout=15), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                        process.stdin.close()
                        process.stdout.close()
                    errors.seek(0)
                    self.assertNotIn("ERROR", errors.read())
                launch.verify_inputs()
                origins = (runtime / "class-load.log").read_text()
                self.assertIn("external.Driver source: file:", origins)
                self.assertIn("dependency.Maths source: file:", origins)
                if plain:
                    self.assertFalse(capture.metrics)
                    self.assertFalse(capture.traces)
                else:
                    (runtime / "plan.properties").write_text("changed=true\n")
                    with self.assertRaisesRegex(ValueError, "configuration changed"):
                        launch.verify_inputs()
            spans = [span for request in capture.traces for resource in request.resource_spans
                     for scope in resource.scope_spans for span in scope.spans]
            functions = [span.name for span in spans if "workload" in span.name]
            self.assertEqual(len(functions), 12)
            self.assertTrue(all("Driver" in name for name in functions))
            runtime = root / "cli"
            result = subprocess.run([sys.executable, "-m", "otelc", "java",
                                     "--manifest", str(manifest), "--runtime", str(runtime),
                                     "--plain", "--", "argument with spaces"],
                                    input="2\nquit\n", capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["checksum"], 3)
            self.assertGreater(json.loads((runtime / "process.json").read_text())["pid"], 0)
            runtime = root / "prepared-json"
            result = subprocess.run([sys.executable, "-m", "otelc", "java", "--manifest", str(manifest),
                                     "--runtime", str(runtime), "--prepare-only"],
                                    capture_output=True, text=True, check=True)
            identity = json.loads(result.stdout)
            self.assertEqual(identity["control_socket"], str(runtime.resolve() / "control.sock"))
            self.assertTrue(any(argument.startswith("-javaagent:") for argument in identity["argv"]))
            self.assertFalse((runtime / "process.json").exists())
            self.assertFalse((runtime / "class-load.log").exists())
            application_jar = root / "application.jar"
            with zipfile.ZipFile(application_jar, "w") as archive:
                for compiled in classes.rglob("*.class"):
                    archive.write(compiled, compiled.relative_to(classes).as_posix())
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "external.Driver",
                                            "classpath": ["application.jar", "dependency.jar"], "bindings": bindings}))
            jar_capture = Capture()
            self.addCleanup(jar_capture.close)
            data["export"]["endpoint"] = f"http://127.0.0.1:{jar_capture.server.server_port}"
            runtime = root / "bound-jar"
            runtime.mkdir(mode=0o700)
            launch = prepared_java.prepare(prepared_java.PreparedApplication.load(manifest),
                                           Policy.parse(data), runtime, arguments=("argument with spaces",))
            with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    control.wait_ready(runtime / "control.sock", process.pid)
                    control.request(runtime / "control.sock", "on")
                    self.assertEqual(batch(process, 2)["checksum"], 3)
                    control.request(runtime / "control.sock", "flush")
                    self.assertEqual(jar_capture.calls(), 2)
                    output, errors = process.communicate("quit\n", timeout=15)
                    self.assertEqual(process.returncode, 0, errors)
                    self.assertEqual(output, "")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            launch.verify_inputs()
            self.assertIn("application.jar", (runtime / "class-load.log").read_text())


if __name__ == "__main__":
    unittest.main()
