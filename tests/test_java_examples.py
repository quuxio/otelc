"""The documented ordinary examples can be built and launched through the public API."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from otelc import java, java_examples, prepared_java
from otelc.config import Policy


class JavaExampleTests(unittest.TestCase):
    def test_generated_manifests_launch_ordinary_examples_and_preserve_source_bindings(self) -> None:
        policy = Policy.load(java.ROOT / "examples/context.toml")
        with tempfile.TemporaryDirectory(prefix="otelc-examples-", dir="/tmp") as directory:
            root = Path(directory)
            manifests = java_examples.build(root / "build")
            self.assertEqual(set(manifests), {"TaskApp"})
            for name, manifest in manifests.items():
                with self.subTest(name=name):
                    application = prepared_java.PreparedApplication.load(manifest)
                    runtime = root / name
                    runtime.mkdir(mode=0o700)
                    launch = prepared_java.prepare(application, policy, runtime, plain=True,
                                                   diagnostic_profile=True)
                    output = subprocess.run(launch.argv, input="quit\n", capture_output=True,
                                            text=True, check=True, timeout=10)
                    self.assertEqual((output.stdout, output.stderr), ("ready\n", ""))
                    launch.verify_inputs()
