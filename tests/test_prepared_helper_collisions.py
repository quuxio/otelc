"""Injected helpers must not replace an application's own classes or methods."""

from __future__ import annotations

import json
import select
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from otelc import control, java, prepared_java
from otelc.config import Policy
from test_java_integration import Capture
from test_prepared_java import binding


class HelperCollisionTests(unittest.TestCase):
    def test_private_helpers_preserve_application_namespaces_without_source_changes(self) -> None:
        for namespace in ("methods", "lifecycle", "witness.probe"):
            for jar in (False, True):
                with self.subTest(namespace=namespace, jar=jar):
                    self.exercise(namespace, jar)

    def exercise(self, namespace: str, jar: bool) -> None:
        capture = Capture()
        self.addCleanup(capture.close)
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "Main.java"
            source.write_text('public class Main { static int run() {'
                ' try (WorkItem item = new WorkItem()) { return io.quux.otelc.' + namespace + '.Observer.applicationValue(); }}'
                ' static final class WorkItem implements AutoCloseable { public void close() {} }'
                ' public static void main(String[] args) throws Exception {'
                ' java.nio.file.Files.writeString(java.nio.file.Path.of(args[0]), "main");'
                ' System.out.println(run()); System.in.read(); }}')
            helper = root / "Observer.java"
            helper_marker = root / "helper-ran"
            helper.write_text('package io.quux.otelc.' + namespace + '; public class Observer {'
                ' static { try { java.nio.file.Files.writeString(java.nio.file.Path.of('
                + json.dumps(str(helper_marker)) + '), "helper"); }'
                ' catch (java.io.IOException error) { throw new java.io.UncheckedIOException(error); } }'
                ' public static int applicationValue() { return 1; }}')
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source), str(helper)], check=True)
            classpath = "classes"
            if jar:
                archive_path = root / "application.jar"
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for path in classes.rglob("*.class"):
                        archive.write(path, path.relative_to(classes).as_posix())
                classpath = "application.jar"
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Main", "classpath": [classpath],
                "bindings": {name: binding(source, classes / f"{name}.class", "src/Main.java")
                             for name in ("Main", "Main$WorkItem")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy = Policy.parse({"schema_version": 3, "languages": ["java"],
                "sources": {"include": ["src/**"]}, "functions": {"include": ["Main.run()"]},
                "lifetimes": {"enabled": True, "include": ["*WorkItem"], "release_methods": ["close"]},
                "traces": {"enabled": True, "sample_ratio": 1.0},
                "export": {"endpoint": f"http://127.0.0.1:{capture.server.server_port}"}})
            for plain in (True, False):
                runtime = root / ("plain" if plain else "instrumented")
                runtime.mkdir(mode=0o700)
                marker = root / "main-ran"
                launch = prepared_java.prepare(application, policy, runtime, plain=plain,
                                               arguments=(str(marker),), diagnostic_profile=True)
                with subprocess.Popen(launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertTrue(select.select([process.stdout], [], [], 15)[0])
                        value = process.stdout.readline()
                        if not plain and value == "1\n":
                            control.wait_ready(runtime / "control.sock", process.pid)
                            status = control.request(runtime / "control.sock", "status")
                            self.assertEqual(status["lifetime_lost"], 0)
                        stdout, stderr = process.communicate("\n", timeout=15)
                        self.assertEqual((process.returncode, value + stdout, stderr), (0, "1\n", ""), stderr)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                self.assertEqual(marker.read_text(), "main")
                self.assertEqual(helper_marker.read_text(), "helper")
                marker.unlink()
                helper_marker.unlink()
                self.assertEqual(len(launch.identity), 16)
                launch.verify_inputs()
                if plain:
                    self.assertFalse((runtime / "helpers-extension.jar").exists())
                else:
                    mapping = json.loads((runtime / "helpers.json").read_text())
                    self.assertEqual(set(mapping["packages"]), {"io.quux.otelc.methods.", "io.quux.otelc.lifecycle.", "io.quux.otelc.witness.probe."})
                    self.assertEqual(capture.calls(), 1)
                    self.assertGreater(len(capture.traces), 0)
