"""Match ordinary script metadata and resource lookup without source edits."""
import json
import py_compile
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_python_adapter import ADAPTER, plan
from test_python_spans import receiver


class ScriptMetadataTests(unittest.TestCase):
    def test_relative_absolute_and_spaced_script_paths_preserve_metadata(self):
        source = '''from pathlib import Path
import json, os, sys
os.chdir("empty")
print(json.dumps({"file":__file__,"argv":sys.argv,"resource":Path(__file__).with_name("resource.txt").read_text(),"package":__package__,"spec":__spec__,"cached":globals().get("__cached__"),"loader":__loader__.name}))
'''
        with tempfile.TemporaryDirectory(prefix="otelc script metadata ") as name:
            root = Path(name)
            (root / "empty").mkdir()
            (root / "resource.txt").write_text("fixture resource")
            script = root / "resource fixture.py"
            script.write_text(source)
            original = script.read_bytes()
            bytecode = script.with_suffix(".pyc")
            py_compile.compile(str(script), cfile=str(bytecode), doraise=True)
            original_bytecode = bytecode.read_bytes()
            with receiver() as (endpoint, _):
                policy = plan()
                policy["metrics_endpoint"] = endpoint + "/v1/metrics"
                policy["metrics"]["enabled"] = False
                config = root / "plan.json"
                config.write_text(json.dumps(policy))
                for argument in (script.name, "./" + script.name, str(script), bytecode.name, str(bytecode)):
                    with self.subTest(argument=argument):
                        plain = subprocess.run([sys.executable, argument, "same argument"], cwd=root, capture_output=True, timeout=10)
                        measured = subprocess.run([sys.executable, str(ADAPTER / "launch.py"), str(config), argument, "same argument"], cwd=root, capture_output=True, timeout=10)
                        self.assertEqual(plain.returncode, 0, plain.stderr)
                        self.assertEqual(measured.returncode, plain.returncode, measured.stderr)
                        self.assertEqual(measured.stdout, plain.stdout)
                        self.assertEqual(measured.stderr, plain.stderr)
                        self.assertEqual(script.read_bytes(), original)
                        self.assertEqual(bytecode.read_bytes(), original_bytecode)
