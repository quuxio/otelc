"""Retain the real application main module and metadata through exit callbacks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import importlib.util

import launch

from test_python_adapter import ADAPTER, plan
from test_python_spans import receiver


class MainNamespaceTests(unittest.TestCase):
    def test_script_module_and_package_keep_globals_classes_and_module_identity(self):
        source = '''import atexit, builtins, importlib, json, pickle, sys
import __main__
VALUE=42
ORIGINAL_MAIN=__main__
class ApplicationValue:
    def __init__(self,value): self.value=value

def cleanup():
    current=importlib.import_module("__main__")
    value=pickle.loads(pickle.dumps(ApplicationValue(VALUE)))
    print(json.dumps({"value":getattr(current,"VALUE","MISSING"),"class":current.ApplicationValue is ApplicationValue,"same":current is ORIGINAL_MAIN,"roundtrip":value.value,"spec":getattr(current.__spec__,"name",None),"package":current.__package__,"argv":sys.argv,"builtins_module":current.__builtins__ is builtins,"launcher_key":"Runtime" in vars(current)}))
atexit.register(cleanup)
if "fail" in sys.argv: raise SystemExit(7)
'''
        with tempfile.TemporaryDirectory(prefix="otelc main namespace ") as name, receiver() as (endpoint,_):
            root=Path(name);script=root/"entry.py";script.write_text(source)
            package=root/"application";package.mkdir()
            (package/"__init__.py").write_text("MARKER='relative import preserved'\n")
            (package/"__main__.py").write_text("from . import MARKER\n"+source)
            directory=root/"directory";directory.mkdir()
            (directory/"__main__.py").write_text(source)
            archive=root/"application.zip"
            with zipfile.ZipFile(archive,"w") as packed:
                packed.writestr("__main__.py",source)
            policy=plan();policy["metrics"]["enabled"]=False
            policy["metrics_endpoint"]=endpoint+"/v1/metrics"
            config=root/"plan.json";config.write_text(json.dumps(policy))
            for invocation in ([script.name],["-m","entry"],["-m","application"],[directory.name],[archive.name]):
                for extra in ([],["fail"]):
                    arguments=[*invocation,*extra]
                    with self.subTest(arguments=arguments):
                        plain=subprocess.run([sys.executable,*arguments],cwd=root,capture_output=True,timeout=10)
                        measured=subprocess.run([sys.executable,str(ADAPTER/"launch.py"),str(config),*arguments],cwd=root,capture_output=True,timeout=10)
                        self.assertEqual(measured.returncode,plain.returncode)
                        self.assertEqual(measured.stderr,plain.stderr)
                        self.assertEqual(measured.stdout,plain.stdout)
                        self.assertEqual(script.read_text(),source)
                        self.assertEqual((package/"__main__.py").read_text(),"from . import MARKER\n"+source)

    def test_missing_or_opaque_entry_points_fail_before_execution(self):
        with self.assertRaisesRegex(ImportError, "no executable main module"):
            launch.module_entry(None)
        spec=importlib.util.find_spec("sys")
        with self.assertRaisesRegex(ImportError, "no executable code"):
            launch.module_entry(spec)
