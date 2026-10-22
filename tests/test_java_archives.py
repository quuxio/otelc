"""Real JVM archive selection must drive metadata, probes and launch identities alike."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from otelc import benchmark, control, java, java_archives, java_helpers, prepared_java
from otelc.config import Policy
from test_java_integration import Capture, batch, wait_for_count
from test_prepared_java import binding


class RuntimeArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="otelc-mr-", dir="/tmp")
        cls.root = Path(cls.temporary.name).resolve()
        version = subprocess.check_output([java.executable("java"), "-version"], stderr=subprocess.STDOUT, text=True)
        cls.feature = int(re.search(r'version "([0-9]+)', version)[1])
        cls.sources: dict[int, Path] = {}
        cls.outputs: dict[int, Path] = {}
        for release in (8, 9, cls.feature):
            directory = cls.root / str(release)
            directory.mkdir()
            source = directory / "App.java"
            helper = directory / "Observer.java"
            helper.write_text('package io.quux.otelc.methods; public class Observer {'
                              'public static int applicationValue() { return 42; }}')
            extra = "+ io.quux.otelc.methods.Observer.applicationValue()" if release == cls.feature else ""
            source.write_text('''package demo;
public final class App {
  static final class WorkItem implements AutoCloseable {
    WorkItem() {} public void close() {}
  }
  static int workload(int n) {
    try (WorkItem item = new WorkItem()) { return n + ''' + str(release) + extra + '''; }
  }
  public static void main(String[] args) throws Exception {
    java.io.BufferedReader reader = new java.io.BufferedReader(new java.io.InputStreamReader(System.in));
    String resource;
    try (java.io.InputStream input = App.class.getResourceAsStream("/value.txt")) {
      resource = new String(input.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8);
    }
    String line;
    while ((line = reader.readLine()) != null && !line.equals("quit")) {
      int count = Integer.parseInt(line); int result = 0;
      for (int n = 0; n < count; n++) result += workload(n);
      System.out.println("{\\"checksum\\":" + result + ",\\"resource\\":\\"" + resource + "\\"}");
    }
  }
}'''.replace('new String(input.readAllBytes(), java.nio.charset.StandardCharsets.UTF_8)',
             'new java.io.BufferedReader(new java.io.InputStreamReader(input, java.nio.charset.StandardCharsets.UTF_8)).readLine()'))
            output = directory / "classes"
            subprocess.run([java.executable("javac"), "--release", str(release), "-d", str(output),
                            str(source), str(helper)], check=True)
            cls.sources[release], cls.outputs[release] = source, output

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.case.cleanup)
        self.directory = Path(self.case.name)

    def archive(self, kind: str) -> Path:
        path = self.directory / (kind + ".jar")
        flag = {"active": "Multi-Release: true\r\n", "identical": "Multi-Release: true\r\n",
                "folded": "Multi-Release: tr\r\n ue\r\n", "inactive": "Multi-Release: false\r\n"}.get(kind, "")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n" + flag + "\r\n")
            archive.write(self.outputs[8] / "demo/App.class", "demo/App.class")
            archive.write(self.outputs[8] / "demo/App$WorkItem.class", "demo/App$WorkItem.class")
            archive.writestr("value.txt", "base")
            if kind != "ordinary":
                archive.write(self.outputs[9] / "demo/App.class", "META-INF/versions/9/demo/App.class")
                current = self.outputs[8] if kind == "identical" else self.outputs[self.feature]
                archive.write(current / "demo/App.class", f"META-INF/versions/{self.feature}/demo/App.class")
                archive.write(current / "demo/App$WorkItem.class", f"META-INF/versions/{self.feature}/demo/App$WorkItem.class")
                archive.write(self.outputs[self.feature] / "io/quux/otelc/methods/Observer.class",
                              f"META-INF/versions/{self.feature}/io/quux/otelc/methods/Observer.class")
                archive.writestr(f"META-INF/versions/{self.feature}/value.txt", "current")
                archive.write(self.outputs[self.feature] / "demo/App.class", f"META-INF/versions/{self.feature + 1}/demo/App.class")
        return path

    def application(self, path: Path, release: int) -> prepared_java.PreparedApplication:
        manifest = self.directory / "application.json"
        manifest.write_text(json.dumps({"schema_version": 1, "main_class": "demo.App", "classpath": [str(path)],
            "bindings": {name: binding(self.sources[release], self.outputs[release] / (name.replace(".", "/") + ".class"), "src/App.java")
                         for name in ("demo.App", "demo.App$WorkItem")}}))
        return prepared_java.PreparedApplication.load(manifest)

    def test_plain_and_instrumented_effective_members_resources_helpers_and_live_states(self) -> None:
        for kind in ("ordinary", "inactive", "active", "folded", "identical"):
            with self.subTest(kind=kind):
                self.exercise(kind)

    def exercise(self, kind: str) -> None:
        path = self.archive(kind)
        inventory = java_archives.resolve((path,))
        archive = inventory["archives"][0]
        selected = archive["classes"]["demo.App"]["entry"]
        release = self.feature if selected.startswith("META-INF/versions/") and kind != "identical" else 8
        application = self.application(path, release)
        # The ordinary JVM is an independent outcome reference, not the instrumented launcher.
        reference = subprocess.run([java.executable("java"), "-cp", str(path), "demo.App"],
                                   input="2\nquit\n", capture_output=True, text=True, check=True)
        expected = json.loads(reference.stdout)
        self.assertEqual(reference.stderr, "")
        capture = Capture()
        self.addCleanup(capture.close)
        policy = Policy.parse({"schema_version": 3, "sources": {"include": ["src/**"]},
            "functions": {"include": ["demo.App.workload(int)"]},
            "lifetimes": {"enabled": True, "include": ["demo.App$WorkItem"], "release_methods": ["close"]},
            "metrics": {"collection": False, "emission": False},
            "traces": {"enabled": True, "sample_ratio": 1.0},
            "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
        for plain in (True, False):
            runtime = self.directory / (kind + ("-plain" if plain else "-instrumented"))
            runtime.mkdir(mode=0o700)
            launch = prepared_java.prepare(application, policy, runtime, plain=plain, diagnostic_profile=True)
            self.assertEqual(launch.identity["schema_version"], 2)
            self.assertEqual(len(launch.identity), 16)
            self.assertEqual(len(launch.identity["application"]), 7)
            self.assertEqual(launch.identity["application"]["archive_resolution"], inventory)
            self.assertEqual(launch.identity["tooling"].count(prepared_java.file_identity(java_archives.tool())), 1)
            javap = str(Path(java.executable("javap")).resolve())
            self.assertEqual(sum(item["path"] == javap for item in launch.identity["tooling"]), 0 if plain else 1)
            self.assertFalse(list(runtime.glob("metadata-*")))
            self.assertEqual(launch.argv[launch.argv.index("-cp") + 1], str(path))
            with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as process:
                try:
                    if not plain:
                        ready = control.wait_ready(runtime / "control.sock", process.pid)
                        self.assertFalse(ready["collection"])
                    for mode, completed in (("both_off", 0), ("both_on", 2), ("collection_only", 4),
                                            ("emission_only", 4), ("both_on", 6)):
                        if not plain:
                            state = benchmark.switch_state(runtime / "control.sock", process, mode)
                            self.assertEqual(state["pid"], process.pid)
                        self.assertEqual(batch(process, 2), expected)
                        if not plain:
                            state = control.request(runtime / "control.sock", "flush")
                            self.assertEqual(state["completed"], completed)
                    output, errors = process.communicate("quit\n", timeout=15)
                    self.assertEqual((process.returncode, output, errors), (0, "", ""))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            launch.verify_inputs()
            self.assertIn(path.name, (runtime / "class-load.log").read_text())
            if not plain:
                self.assertEqual((runtime / "helpers.json").exists(), archive["multi_release"])
                self.assertEqual(wait_for_count(capture.calls, 6), 6)
                resources = lambda: capture.lifetime_count("otelc.lifetime.completed", "resource", "demo.App$WorkItem")
                self.assertEqual(wait_for_count(resources, 6), 6)
                spans = [span for request in capture.traces for resource in request.resource_spans
                         for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual(len([span for span in spans if span.name == "App.workload"]), 10)
                lifetimes = [span for span in spans if any(item.key == "boundary" and item.value.string_value == "resource" for item in span.attributes)]
                self.assertEqual(len(lifetimes), 10)
            else:
                self.assertFalse(capture.metrics)
                self.assertFalse(capture.traces)

    def test_same_bytes_keep_real_member_identity_and_modified_claims_fail(self) -> None:
        path = self.archive("identical")
        application = self.application(path, 8)
        runtime = self.directory / "runtime"
        runtime.mkdir(mode=0o700)
        launch = prepared_java.prepare(application, Policy.parse({"schema_version": 3}), runtime, plain=True)
        record = launch.identity["application"]["archive_resolution"]
        member = record["archives"][0]["classes"]["demo.App"]
        self.assertEqual(member["entry"], f"META-INF/versions/{self.feature}/demo/App.class")
        self.assertEqual(member["sha256"], binding(self.sources[8], self.outputs[8] / "demo/App.class", "src/App.java")["class_sha256"])
        member["entry"] = "demo/App.class"
        with self.assertRaisesRegex(ValueError, "application inputs changed"):
            launch.verify_inputs()
        member["entry"] = f"META-INF/versions/{self.feature}/demo/App.class"
        record["jdk_feature"] += 1
        with self.assertRaisesRegex(ValueError, "application inputs changed"):
            launch.verify_inputs()

    def test_source_bindings_reject_stale_base_class_and_raw_archive_changes(self) -> None:
        path = self.archive("active")
        with self.assertRaisesRegex(ValueError, "stale class binding"):
            self.application(path, 8)
        application = self.application(path, self.feature)
        runtime = self.directory / "runtime"
        runtime.mkdir(mode=0o700)
        launch = prepared_java.prepare(application, Policy.parse({"schema_version": 3}), runtime, plain=True)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("unused-resource.txt", "changed")
        with self.assertRaisesRegex(ValueError, "application inputs changed"):
            launch.verify_inputs()

    def test_metadata_requires_the_same_selected_full_jdk(self) -> None:
        application = self.application(self.archive("ordinary"), 8)
        runtime = self.directory / "runtime"
        runtime.mkdir(mode=0o700)
        original = java.executable
        with patch("otelc.java.executable", side_effect=lambda name: "/usr/bin/true" if name == "javap" else original(name)):
            with self.assertRaisesRegex(ValueError, "same full JDK"):
                prepared_java.prepare(application, Policy.parse({"schema_version": 3}), runtime)
        self.assertFalse((runtime / "launch.json").exists())

    def test_selected_return_metadata_uses_runtime_member_before_main_executes(self) -> None:
        sources, outputs = {}, {}
        for release, returned in ((8, "Unavailable"), (self.feature, "Object")):
            directory = self.directory / str(release)
            directory.mkdir()
            source = directory / "Returns.java"
            source.write_text('class Unavailable {} public class Returns {'
                ' static ' + returned + ' chosen() { return null; }'
                ' public static void main(String[] args) throws Exception {'
                ' java.nio.file.Files.write(java.nio.file.Paths.get(args[0]), new byte[] {1});'
                ' System.out.println(chosen() == null); }}')
            output = directory / "classes"
            subprocess.run([java.executable("javac"), "--release", str(release), "-d", str(output), str(source)], check=True)
            sources[release], outputs[release] = source, output
        policy = Policy.parse({"schema_version": 3, "sources": {"include": ["src/**"]},
                               "functions": {"include": ["Returns.chosen()"]}})
        for kind, flag in (("active", "true\r\n"), ("folded", "tr\r\n ue\r\n")):
            with self.subTest(kind=kind):
                path = self.directory / (kind + ".jar")
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\nMulti-Release: " + flag + "\r\n")
                    archive.write(outputs[8] / "Returns.class", "Returns.class")
                    archive.write(outputs[self.feature] / "Returns.class", f"META-INF/versions/{self.feature}/Returns.class")
                resolution = java_archives.resolve((path,))
                active = resolution["archives"][0]["multi_release"]
                release = self.feature if active else 8
                manifest = self.directory / "application.json"
                manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Returns", "classpath": [str(path)],
                    "bindings": {"Returns": binding(sources[release], outputs[release] / "Returns.class", "src/Returns.java")}}))
                application = prepared_java.PreparedApplication.load(manifest)
                marker = self.directory / "main-ran"
                plain = subprocess.run([java.executable("java"), "-cp", str(path), "Returns", str(marker)],
                                       capture_output=True, text=True, check=True)
                self.assertEqual((plain.stdout, plain.stderr), ("true\n", ""))
                self.assertTrue(marker.exists())
                marker.unlink()
                runtime = self.directory / (kind + "-return")
                runtime.mkdir(mode=0o700)
                if active:
                    capture = Capture()
                    self.addCleanup(capture.close)
                    data = copy.deepcopy(policy.data)
                    data["export"]["endpoint"] = f"http://127.0.0.1:{capture.server.server_port}"
                    launch = prepared_java.prepare(application, Policy.parse(data), runtime,
                                                   arguments=(str(marker),), diagnostic_profile=True)
                    self.assertFalse(marker.exists(), "preflight must not execute the application")
                    result = subprocess.run(launch.argv, capture_output=True, text=True, check=True, timeout=15)
                    self.assertEqual((result.stdout, result.stderr), ("true\n", ""))
                    launch.verify_inputs()
                    marker.unlink()
                else:
                    with self.assertRaisesRegex(ValueError, "selected Java return-type metadata is unavailable"):
                        prepared_java.prepare(application, policy, runtime, arguments=(str(marker),))
                    self.assertFalse(marker.exists())
                    self.assertFalse(list(runtime.glob("metadata-*")))

    def test_exact_metadata_bytes_and_classpath_positions_ignore_javap_mr_parser(self) -> None:
        path = self.archive("folded")
        ordinary = self.archive("ordinary")
        # Use a resource-only ordinary archive to keep the prepared class inventory unambiguous.
        with zipfile.ZipFile(ordinary, "w") as archive:
            archive.writestr("extra.txt", "kept")
        first, middle = self.directory / "first", self.directory / "middle"
        first.mkdir(); middle.mkdir()
        classpath = (first, path, middle, ordinary)
        resolution = java_archives.resolve(classpath)
        self.assertEqual([archive["classpath_index"] for archive in resolution["archives"]], [1, 3])
        names = java_helpers.class_names(list(classpath))
        self.assertIn("demo.App", names)
        self.assertFalse(any("META-INF" in name for name in names))
        runtime = self.directory / "runtime"
        runtime.mkdir(mode=0o700)
        with java_archives.metadata_classpath(classpath, resolution, runtime) as views:
            self.assertEqual((views[0], views[2]), (first, middle))
            member = resolution["archives"][0]["classes"]["demo.App"]
            self.assertEqual(hashlib.sha256((views[1] / "demo/App.class").read_bytes()).hexdigest(), member["sha256"])
            output = subprocess.check_output([java.executable("javap"), "-c", "-classpath", os.pathsep.join(map(str, views)), "demo.App"], text=True)
            self.assertIn("bipush        " + ("21" if resolution["archives"][0]["multi_release"] else "8"), output)
        self.assertFalse(views[1].exists())

    def test_resolver_refuses_invalid_protocol_paths_and_unverified_metadata(self) -> None:
        path = self.archive("active")
        resolution = java_archives.resolve((path,))
        record = resolution["archives"][0]
        identities = {str(path): prepared_java.file_identity(path)}
        protocol = {"schema_version": 1, "jdk_feature": self.feature,
                    "archives": [{key: value for key, value in record.items() if key != "classpath_index"} | {"versioned_members": True}]}
        cases = [("schema_version", True), ("jdk_feature", self.feature + 1), ("archives", []), ("extra", 1)]
        for key, value in cases:
            candidate = copy.deepcopy(protocol)
            candidate[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                java_archives.validate_protocol(candidate, [(0, path)], identities, self.feature)
        for changes in ({"path": "wrong"}, {"sha256": "0" * 64}, {"sha256": "short"},
                        {"multi_release": 1}, {"versioned_members": 1}, {"classes": []},
                        {"classes": {"../escape": {"entry": "../escape.class", "sha256": "0" * 64}}},
                        {"classes": {"demo.App": {"entry": "META-INF/versions/999/demo/App.class", "sha256": "0" * 64}}},
                        {"classes": {"demo.App": {"entry": 1, "sha256": "0" * 64}}}):
            candidate = copy.deepcopy(protocol)
            candidate["archives"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                java_archives.validate_protocol(candidate, [(0, path)], identities, self.feature)
        for name in ("", ".App", "a..App", "a/App", "a\\App", "C:App", "a\nApp", "META-INF.Version", "module-info"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                java_archives.class_path(name)
        original_sha = record["sha256"]
        record["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "changed before metadata"):
            java_archives.copy_members(record, self.directory / "view")
        record["sha256"] = original_sha
        member = record["classes"]["demo.App"]
        original_entry = member["entry"]
        member["entry"] = "absent.class"
        with self.assertRaisesRegex(ValueError, "invalid effective runtime archive member"):
            java_archives.copy_members(record, self.directory / "view")
        member["entry"] = original_entry
        record["classes"]["demo.App"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "member digest changed"):
            java_archives.copy_members(record, self.directory / "view")
        with patch.dict(os.environ, {"JDK_JAVA_OPTIONS": "-Djdk.util.jar.version=8"}):
            with self.assertRaisesRegex(ValueError, "ambient JVM"):
                java_archives.resolve((path,))


if __name__ == "__main__":
    unittest.main()
