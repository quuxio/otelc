"""Public cache-origin launches use unchanged apps, explicit formats and live controls."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import py_compile
import subprocess
import sys
import time
from unittest import mock

from ci import build_python_inputs as tooling
from otelc import prepared_python as prepared, python_recipe as recipe, control
from otelc import python_launch_files as files
from otelc import python_origin_journal as journal
from otelc import python_origin_execution as execution
from otelc.config import Policy
from test_python_prepared_origins import PreparedOriginCase

PROFILE = 'python-native-cache-origins-v3'


class PreparedPythonCachedOriginTests(PreparedOriginCase):
    profile = PROFILE

    def cached_target(self) -> tuple[Path, Path]:
        target = self.app / 'target.py'
        target.write_text('def workload(value):return value*2+3\n')
        cache = Path(py_compile.compile(str(target), doraise=True))
        return target, cache

    def test_explicit_cached_formats_bind_tool_and_root_without_startup(self) -> None:
        value = prepared.protocols(profile=PROFILE)
        self.assertEqual(value['schema_version'], 3)
        self.assertEqual(value['profile'], PROFILE)
        self.assertEqual(value['journal'], {'schema_version': 3, 'record_kinds': list(range(1,8))})
        self.assertIsNone(value['code_comparisons'])
        self.assertIs(value['source_complete'], False)
        runtime, result = self.prepare()
        for name in ('request','plan','prepared'):
            self.assertEqual(result[name]['schema_version'], 3)
        self.assertEqual(prepared.verify(runtime), (result['request'],result['plan'],result['prepared']))
        command = recipe.native_command(result['request'], prepared.native_tool(PROFILE), 17)
        self.assertEqual(command[1:4], ['--origin-loader-caches-root',str(self.app),'17'])
        self.assertFalse((runtime/'sdk').exists())
        self.assertFalse((runtime/'python-inputs.journal').exists())

    def test_unchanged_existing_cache_has_actual_origins_and_sdk_joins_in_both_lanes(self) -> None:
        target, cache = self.cached_target()
        self.entry.write_text('import fractions\nfrom target import workload\nprint(workload(7))\n')
        original = {path:path.read_bytes() for path in (self.entry,target,cache)}
        stock = subprocess.run([sys.executable,str(self.entry)],cwd=self.app,capture_output=True,timeout=20)
        self.assertEqual((stock.returncode,stock.stdout,stock.stderr),(0,b'17\n',b''))
        for plain in (True,False):
            with self.subTest(plain=plain):
                runtime,result = self.prepare(plain=plain)
                child = subprocess.run(result['command'],capture_output=True,timeout=30)
                self.assertEqual((child.returncode,child.stdout,child.stderr),(0,b'17\n',b''))
                report = json.loads((runtime/'python-execution.json').read_bytes())
                self.assertEqual((report['schema_version'],report['phase']),(3,'closed'))
                self.assertEqual(report['entry_evidence']['phase'],'complete')
                self.assertEqual(report['entry_evidence']['origins']['schema_version'],3)
                value = journal.read(runtime/'python-inputs.journal',expected_pid=report['application']['pid'],version=3)
                self.assertTrue(value.capture_complete)
                self.assertEqual([row.route for row in value.roots],[1,3])
                self.assertEqual(len(value.sdk_joins),0 if plain else 1)
                self.assertEqual(len(value.codes),0 if plain else 1)
                self.assertEqual({path:path.read_bytes() for path in original},original)
                with self.assertRaises(ValueError):
                    journal.read(runtime/'python-inputs.journal',expected_pid=report['application']['pid'])
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans),1)
        self.assertEqual({item.key:item.value for item in spans[0].attributes}['otelc.python.code_id'].int_value,
                         value.sdk_joins[0][0])
        self.assertEqual(self.capture.calls(),1)

    def test_existing_cache_and_code_identity_survive_all_live_metric_states(self) -> None:
        target,cache = self.cached_target()
        original = {path:path.read_bytes() for path in (target,cache)}
        data = copy.deepcopy(self.policy.data)
        data['metrics'].update(collection=False,emission=False)
        self.policy = Policy.parse(data)
        self.entry.write_text('''import sys
from target import workload
for line in sys.stdin:
    if line.strip()=='quit':break
    print(workload(int(line)),flush=True)
''')
        runtime,result = self.prepare()
        process = subprocess.Popen(result['command'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,start_new_session=True)
        pids=set()
        try:
            deadline=time.monotonic()+15
            while True:
                try:
                    application,_=files.read(runtime/'python-application-start.json')
                    break
                except FileNotFoundError:
                    if process.poll() is not None or time.monotonic()>=deadline:
                        raise RuntimeError('application startup record unavailable')
                    time.sleep(.01)
            endpoint=runtime/'control.json'
            control.wait_ready(endpoint,application['application_pid'])
            for value,(collection,emission) in enumerate(((False,False),(True,False),(False,True),(True,True),(False,False))):
                state=control.request(endpoint,'collection '+('on' if collection else 'off'))
                pids.add(state['pid'])
                state=control.request(endpoint,'emission '+('on' if emission else 'off'))
                self.assertEqual((state['collection'],state['emission']),(collection,emission))
                process.stdin.write(str(value).encode()+b'\n');process.stdin.flush()
                self.assertEqual(process.stdout.readline(),str(value*2+3).encode()+b'\n')
                control.request(endpoint,'flush')
            process.stdin.write(b'quit\n');process.stdin.flush()
            out,err=process.communicate(timeout=30)
            self.assertEqual((process.returncode,out,err),(0,b'',b''))
        finally:
            if process.poll() is None:
                prepared.stop_group(process)
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream is not None and not stream.closed:stream.close()
        report=json.loads((runtime/'python-execution.json').read_bytes())
        self.assertEqual(report['phase'],'closed')
        self.assertEqual(pids,{report['application']['pid']})
        value=journal.read(runtime/'python-inputs.journal',expected_pid=report['application']['pid'],version=3)
        self.assertTrue(value.capture_complete)
        self.assertEqual(len(value.sdk_joins),1)
        self.assertEqual(len([root for root in value.roots if root.route==3]),1)
        spans=[span for request in self.capture.traces for resource in request.resource_spans
               for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans),5)
        for span in spans:
            self.assertEqual({item.key:item.value for item in span.attributes}['otelc.python.code_id'].int_value,
                             value.sdk_joins[0][0])
        self.assertEqual(self.capture.calls(),2)
        self.assertEqual({path:path.read_bytes() for path in original},original)

    def test_substituted_cached_loader_cannot_report_complete_public_evidence(self) -> None:
        self.cached_target()
        self.entry.write_text('''import marshal
original=marshal.loads
marshal.loads=lambda data:original(data)
from target import workload
print(workload(7))
''')
        for plain in (True,False):
            with self.subTest(plain=plain):
                runtime,result=self.prepare(plain=plain)
                child=subprocess.run(result['command'],capture_output=True,timeout=30)
                self.assertEqual((child.returncode,child.stdout,child.stderr),(1,b'17\n',b''))
                report=json.loads((runtime/'python-execution.json').read_bytes())
                self.assertEqual(report['application']['returncode'],0)
                self.assertEqual(report['phase'],'failed')
                self.assertFalse(report['source_complete'])
                self.assertEqual(report['entry_evidence']['phase'],'failed')

    def test_cached_profile_refuses_module_entries_unavailable_images_and_metadata_downgrades(self) -> None:
        with self.assertRaisesRegex(ValueError,'source entry'):
            prepared.prepare(self.policy,self.root/'module',root=self.app,module='entry',profile=PROFILE)
        with mock.patch.object(tooling,'toolchain',return_value=({'origin_chain':{'available':False}},[])):
            with self.assertRaisesRegex(ValueError,'unavailable'):
                prepared.native_tool(PROFILE)
        runtime,result=self.prepare()
        original=(runtime/'python-request.json').read_bytes()
        changed=copy.deepcopy(result['request'])
        changed['schema_version']=2
        (runtime/'python-request.json').write_text(recipe.encoded(changed))
        with self.assertRaises(ValueError):prepared.verify(runtime)
        (runtime/'python-request.json').write_bytes(original)
        self.assertEqual(prepared.verify(runtime),(result['request'],result['plan'],result['prepared']))
        self.assertFalse((runtime/'python-inputs.journal').exists())

    def test_public_cli_explicit_profile_and_literal_application_arguments(self) -> None:
        self.cached_target()
        marker = self.root / 'application-started'
        self.entry.write_text(f'''import sys,json
from pathlib import Path
from target import workload
Path({str(marker)!r}).write_text('actual application started')
print(json.dumps({{'value':workload(7),'argv':sys.argv[1:]}}))
''')
        config = self.root / 'policy.toml'
        config.write_text('''schema_version=3
languages=["python"]
[sources]
include=["*.py"]
[functions]
include=["*.workload"]
[traces]
enabled=true
sample_ratio=1.0
[export]
endpoint="http://127.0.0.1:''' + str(self.capture.server.server_port) + '''"
interval_ms=100000
timeout_ms=1000
''')
        runtime = self.root / 'cli-runtime'
        cli = [sys.executable, '-I', '-S', '-B', str(prepared.ROOT / 'otelc/cli_bootstrap.py'),
               '--config', str(config), 'python', '--root', str(self.app), '--entry', 'entry.py',
               '--runtime', str(runtime), '--profile', PROFILE, '--prepare-only', '--', '--profile', 'literal-argument']
        child = subprocess.run(cli, capture_output=True, timeout=30)
        self.assertEqual((child.returncode,child.stderr),(0,b''))
        metadata = json.loads(child.stdout)
        self.assertEqual(metadata['schema_version'],3)
        self.assertEqual(metadata['request']['profile'],PROFILE)
        self.assertEqual(metadata['request']['arguments'],['--profile','literal-argument'])
        self.assertFalse(marker.exists())
        self.assertFalse((runtime/'sdk').exists())
        prepared.verify(runtime)
        child = subprocess.run(prepared.invocation(runtime),capture_output=True,timeout=30)
        self.assertEqual((child.returncode,child.stderr),(0,b''))
        self.assertEqual(json.loads(child.stdout),{'value':17,'argv':['--profile','literal-argument']})
        self.assertTrue(marker.exists())
        report = json.loads((runtime/'python-execution.json').read_bytes())
        self.assertEqual((report['schema_version'],report['phase']),(3,'closed'))
        wrong = self.root / 'bad-cli-runtime'
        candidate = cli[:]
        candidate[candidate.index(str(runtime))] = str(wrong)
        candidate[candidate.index(PROFILE)] = 'unknown-profile'
        child = subprocess.run(candidate,capture_output=True,timeout=20)
        self.assertEqual(child.returncode,2)
        self.assertFalse(wrong.exists())

    def test_one_common_example_policy_runs_the_original_shared_workload_from_cache(self) -> None:
        source = prepared.ROOT / 'examples/python/BenchmarkApp.py'
        entry = prepared.ROOT / 'examples/python/CachedBenchmarkApp.py'
        cache = Path(py_compile.compile(str(source),doraise=True))
        original = {path:path.read_bytes() for path in (source,entry,cache)}
        data = copy.deepcopy(Policy.load(prepared.ROOT/'examples/otelc.toml').data)
        data['export']['endpoint'] = f'http://127.0.0.1:{self.capture.server.server_port}'
        data['traces']['sample_ratio'] = 1.0
        policy = Policy.parse(data)
        runtime = self.root / 'common-example'
        prepared.prepare(policy,runtime,root=prepared.ROOT,entry='examples/python/CachedBenchmarkApp.py',profile=PROFILE)
        child = subprocess.run(prepared.invocation(runtime),input=b'3\nthrow\nquit\n',capture_output=True,timeout=30)
        self.assertEqual((child.returncode,child.stderr),(0,b''))
        normal,error = map(json.loads,child.stdout.splitlines())
        self.assertEqual((normal['checksum'],normal['calls']),(12598976,3))
        self.assertGreater(normal['elapsed_ns'],0)
        self.assertEqual(error,{'exception':'ValueError','message':'negative seed'})
        report = json.loads((runtime/'python-execution.json').read_bytes())
        self.assertEqual(report['phase'],'closed')
        self.assertEqual(report['entry_evidence']['phase'],'complete')
        observed = journal.read(runtime/'python-inputs.journal',expected_pid=report['application']['pid'],version=3)
        self.assertTrue(observed.capture_complete)
        self.assertEqual(len([root for root in observed.roots if root.route==3]),1)
        self.assertEqual(self.capture.calls(),4)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans),10)
        functions = [span for span in spans if span.name.endswith('.workload')]
        self.assertEqual(len(functions),4)
        self.assertEqual(sum(span.status.code==2 for span in functions),1)
        boundaries = [item.value.string_value for span in spans for item in span.attributes if item.key=='boundary']
        self.assertEqual(sorted(boundaries),['observed_gc']*3+['resource']*3)
        self.assertEqual({path:path.read_bytes() for path in original},original)
