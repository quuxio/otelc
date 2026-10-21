"""Refuse unavailable selected return metadata before an uninstrumented app can run."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from otelc import java, prepared_java
from otelc.config import Policy
from test_prepared_java import binding


class ReturnMetadataTests(unittest.TestCase):
    def test_reads_only_selected_reference_return_descriptors(self) -> None:
        output = '''public class Example {
  public Example();
    descriptor: ()V
  java.lang.String chosen() throws java.lang.Exception;
    descriptor: ()Ljava/lang/String;
  java.lang.Object[][] chosen(int);
    descriptor: (I)[[Ljava/lang/Object;
  int[] chosen(long);
    descriptor: (J)[I
  Missing ignored();
    descriptor: ()LMissing;
}'''
        self.assertEqual(java.selected_return_types(output, ["chosen"]), {"java.lang.String", "java.lang.Object"})
        self.assertEqual(java.selected_return_types(output, []), set())

    def test_classfile_names_cannot_become_tool_options_or_argument_files(self) -> None:
        for name in ("-J-Xmx8m", "@arguments", ""):
            output = f"  Object chosen();\n    descriptor: ()L{name};\n"
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "invalid selected Java return type"):
                java.selected_return_types(output, ["chosen"])

    def test_cli_refuses_missing_selected_return_metadata_before_starting_main(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            classes = root / "classes"
            classes.mkdir()
            source = root / "Runner.java"
            source.write_text('''class OptionalType {}
public final class Runner {
  static OptionalType broken() { return null; }
  public static void main(String[] args) throws Exception {
    java.nio.file.Files.writeString(java.nio.file.Path.of(args[0]), "started");
    System.out.println(broken() == null);
  }
}''')
            subprocess.run([java.executable("javac"), "-d", str(classes), str(source)], check=True)
            (classes / "OptionalType.class").unlink()
            manifest = root / "application.json"
            manifest.write_text(json.dumps({"schema_version": 1, "main_class": "Runner", "classpath": ["classes"],
                "bindings": {"Runner": binding(source, classes / "Runner.class", "src/Runner.java")}}))
            application = prepared_java.PreparedApplication.load(manifest)
            policy_path = root / "otelc.toml"
            policy_path.write_text('''schema_version = 3
languages = ["java"]
[sources]
include = ["src/**"]
[functions]
include = ["Runner.broken()"]
[traces]
enabled = false
[metrics]
collection = false
emission = false
''')
            marker = root / "started"
            plain_runtime = root / "plain"
            plain_runtime.mkdir(mode=0o700)
            plain = prepared_java.prepare(application, Policy.load(policy_path), plain_runtime,
                                          plain=True, arguments=(str(marker),), diagnostic_profile=True)
            baseline = subprocess.run(plain.argv, capture_output=True, text=True, timeout=15)
            self.assertEqual((baseline.returncode, baseline.stdout, baseline.stderr), (0, "true\n", ""))
            self.assertTrue(marker.exists())
            plain.verify_inputs()
            marker.unlink()
            result = subprocess.run([sys.executable, "-m", "otelc", "--config", str(policy_path), "java",
                "--manifest", str(manifest), "--runtime", str(root / "instrumented"), "--diagnostic-profile", "--", str(marker)],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn("selected Java return-type metadata is unavailable", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse(marker.exists(), "refusal must precede application execution")
            plain.verify_inputs()
