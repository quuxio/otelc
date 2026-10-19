"""Keep monitoring through CPython's normal thread and exit-callback lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from test_python_adapter import ADAPTER, plan
from test_python_spans import receiver


class ProcessLifecycleTests(unittest.TestCase):
    def test_application_threads_and_exit_callbacks_finish_before_final_export(self):
        source = '''import atexit, sys, threading, time
started=threading.Event()
def worker():
    started.set()
    time.sleep(0.15)
    print("worker complete")
def cleanup():
    print("application cleanup")
atexit.register(cleanup)
threading.Thread(target=worker).start()
started.wait()
if "fail" in sys.argv:
    raise SystemExit(7)
'''
        with tempfile.TemporaryDirectory(prefix="otelc lifecycle ") as name:
            root=Path(name)
            script=root/"background.py"
            script.write_text(source)
            for arguments in ([script.name], ["-m", "background"], [script.name, "fail"]):
                with self.subTest(arguments=arguments), receiver() as (endpoint, received):
                    policy=plan()
                    policy["source_matchers"]={"include":["(?-u).*"],"exclude":[]}
                    policy["function_matchers"]={"include":["(?-u)^background\\.(worker|cleanup)$"],"exclude":[]}
                    policy["metrics_endpoint"]=endpoint+"/v1/metrics"
                    policy["runtime"]["shutdown_timeout_ms"]=1000
                    config=root/"plan.json";config.write_text(json.dumps(policy))
                    report=root/"runtime.json";report.unlink(missing_ok=True)
                    plain=subprocess.run([sys.executable,*arguments],cwd=root,capture_output=True,timeout=10)
                    measured=subprocess.run([sys.executable,str(ADAPTER/"launch.py"),str(config),*arguments],cwd=root,env={**os.environ,"OTELC_REPORT_PATH":str(report)},capture_output=True,timeout=10)
                    self.assertEqual((measured.returncode,measured.stdout,measured.stderr),(plain.returncode,plain.stdout,plain.stderr))
                    health=json.loads(report.read_text())
                    self.assertEqual(health["function_calls"],2)
                    self.assertEqual(health["losses"]["incomplete"],0)
                    self.assertEqual(health["export_loss"],0)
                    self.assertTrue(health["export_finished"])
                    names=set()
                    for _,_,body in received:
                        decoded=ExportMetricsServiceRequest.FromString(body)
                        for resource in decoded.resource_metrics:
                            for scope in resource.scope_metrics:
                                for metric in scope.metrics:
                                    if metric.name=="otelc.function.calls":
                                        for point in metric.sum.data_points:
                                            if point.as_int==1:
                                                names.update(attribute.value.string_value for attribute in point.attributes if attribute.key=="code.function.name")
                    self.assertEqual(names,{"background.worker","background.cleanup"})
                    self.assertEqual(script.read_text(),source)

    def test_daemon_work_remains_incomplete_without_delaying_process_exit(self):
        source = '''import threading
started=threading.Event()
stop=threading.Event()
def worker():
    started.set()
    stop.wait()
threading.Thread(target=worker,daemon=True).start()
started.wait()
print("main complete")
'''
        with tempfile.TemporaryDirectory(prefix="otelc daemon ") as name, receiver() as (endpoint,_):
            root=Path(name);script=root/"background.py";script.write_text(source)
            policy=plan()
            policy["source_matchers"]={"include":["(?-u).*"],"exclude":[]}
            policy["function_matchers"]={"include":["(?-u)^background\\.worker$"],"exclude":[]}
            policy["metrics_endpoint"]=endpoint+"/v1/metrics"
            policy["runtime"]["shutdown_timeout_ms"]=500
            config=root/"plan.json";config.write_text(json.dumps(policy))
            report=root/"report.json"
            plain=subprocess.run([sys.executable,script.name],cwd=root,capture_output=True,timeout=5)
            start=time.monotonic()
            measured=subprocess.run([sys.executable,str(ADAPTER/"launch.py"),str(config),script.name],cwd=root,env={**os.environ,"OTELC_REPORT_PATH":str(report)},capture_output=True,timeout=5)
            self.assertLess(time.monotonic()-start,2)
            self.assertEqual((measured.returncode,measured.stdout,measured.stderr),(plain.returncode,plain.stdout,plain.stderr))
            health=json.loads(report.read_text())
            self.assertEqual(health["function_calls"],0)
            self.assertEqual(health["losses"]["incomplete"],1)
            self.assertTrue(health["export_finished"])
            self.assertEqual(script.read_text(),source)
