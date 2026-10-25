"""Public config-only C++ object builds, owned execution and actual OTLP receipt."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time
import unittest

from otelc import control, native_build, native_gate, native_launch, native_object_build, prepared_native
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]
MODEL = '''namespace demo {
struct Item {
 int value;
 explicit Item(int input):value(input){if(input<0)throw input;}
 ~Item() noexcept(false){if(value==13)throw 42;}
};
}
'''
WORK = '''static demo::Item* held=nullptr;
extern "C" __attribute__((noinline)) int workload(int input){
 if(input==20){held=new demo::Item(input);return input;}
 if(input==21){delete held;held=nullptr;return input;}
 demo::Item local(input);return input*3+1;
}
'''
MAIN = '''#include <stdio.h>
extern "C" int workload(int);
int main(){int value;while(scanf("%d",&value)==1){
 try{printf("%d\\n",workload(value));}catch(int failure){printf("caught %d\\n",failure);}
 fflush(stdout);}return 0;}
'''


class PublicLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.temporary = tempfile.TemporaryDirectory(prefix='public-object-tests-', dir=ROOT / 'build', delete=not cls.keep)
        cls.base = Path(cls.temporary.name).resolve()
        cls.sdk = Path(os.environ.get('OTELC_PUBLIC_OBJECT_SDK', str(cls.base / 'sdk')))
        if not os.environ.get('OTELC_PUBLIC_OBJECT_SDK'):
            native_launch.build_sdk(cls.sdk)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.keep:
            cls.temporary._finalizer.detach()
        else:
            cls.temporary.cleanup()

    def setUp(self) -> None:
        self.base = type(self).base / self._testMethodName
        self.base.mkdir()
        self.root = self.base / 'application'; self.root.mkdir()
        self.capture = Capture(); self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['cpp', 'c'],
            'sources': {'include': ['**']}, 'functions': {'include': ['*workload*']},
            'lifetimes': {'enabled': True, 'include': ['demo::Item']},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000}})

    def build(self, *, recipe=False, cli=False, text=None):
        output = self.base / 'compiled'
        source = self.root / 'main.cpp'
        if not recipe:
            source.write_text(text if text is not None else MODEL + WORK + MAIN)
            sources, selected = [source], None
        else:
            (self.root / 'unit').mkdir(); (self.root / 'include').mkdir(); (self.root / 'c').mkdir()
            header = self.root / 'include/item.hpp'; header.write_text(MODEL)
            work = self.root / 'unit/work.cpp'; work.write_text('#include "item.hpp"\n' + WORK)
            source.write_text(MAIN)
            c_source = self.root / 'c/plain.c'; c_source.write_text('int c_application(int x){return x+1;}\n')
            private = self.base / 'private'; private.mkdir()
            overlay_source = private / 'work.cpp'
            overlay_source.write_bytes(work.read_bytes() + b'extern "C" __attribute__((no_profile_instrument_function)) '
                b'int observed(){demo::Item observer_only(4);return observer_only.value;}\n')
            overlay = private / 'overlay.json'
            overlay.write_text(json.dumps({'version': 0, 'use-external-names': False, 'roots': [
                {'type': 'file', 'name': str(work), 'external-contents': str(overlay_source)}]}) + '\n')
            driver = private / 'driver.cpp'
            driver.write_text('extern "C" int __oteleq_original_main();extern "C" int observed();'
                              'int main(){return observed()==4?__oteleq_original_main():91;}\n')
            def unit(identifier, role, path, language, directory, flags, overlay=None, rename=False):
                return {'id': identifier, 'role': role, 'source': str(path), 'language': language,
                        'directory': str(directory), 'flags': flags,
                        'overlay': str(overlay) if overlay else None, 'rename_main': rename}
            selected = {'schema_version': 1, 'kind': 'otelc.native.unit-recipe',
                'assets': list(map(str, (overlay_source, overlay, driver))),
                'units': [unit('main', 'application', source, 'cpp', self.root, ['-std=c++17', '-O0'], rename=True),
                          unit('work', 'application', work, 'cpp', work.parent,
                               ['-std=c++20', '-O1', '-isystem', '../include'], overlay=overlay),
                          unit('cpart', 'application', c_source, 'c', c_source.parent, ['-std=c11', '-O1']),
                          unit('driver', 'driver', driver, 'cpp', private, ['-std=c++17', '-O1'])],
                'link': {'language': 'cpp', 'directory': str(self.root), 'flags': ['-pthread']}}
            sources = []
        before = {str(path): native_gate.identity(path) for path in self.root.rglob('*') if path.is_file()}
        self.original_sources = before
        if cli:
            config = self.base / 'policy.toml'
            config.write_text('schema_version=3\nlanguages=["cpp"]\n[sources]\ninclude=["**"]\n'
                '[functions]\ninclude=["*workload*"]\n'
                '[lifetimes]\nenabled=true\ninclude=["demo::Item"]\n'
                '[metrics]\ncollection=false\nemission=false\n[traces]\nenabled=true\nsample_ratio=1\n'
                f'[export]\nendpoint="http://127.0.0.1:{self.capture.server.server_port}"\n'
                'interval_ms=60000\ntimeout_ms=1000\nshutdown_timeout_ms=5000\n')
            self.policy = Policy.load(config)
            subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(config), 'native-build',
                '--language', 'cpp', '--root', str(self.root), '--source', str(source),
                '--output', str(output), '--sdk-build', str(self.sdk)],
                env=dict(os.environ, OTELC_PYTHON=os.sys.executable), capture_output=True, check=True, timeout=180)
        else:
            native_build.build(self.policy, 'cpp', self.root, sources, output,
                               sdk_directory=self.sdk, recipe=selected)
        built, _, manifest = native_build.verify(output, self.policy)
        self.assertEqual(manifest['schema_version'], 4)
        self.assertEqual(built.manifest()['profile'], native_object_build.PROFILE)
        self.assertEqual({str(path): native_gate.identity(path) for path in self.root.rglob('*') if path.is_file()}, before)
        return output, built

    def start(self, output, name, *, plain=False):
        runtime = self.base / name
        prepared_native.prepare(self.policy, runtime, build_directory=output, plain=plain)
        process = subprocess.Popen(prepared_native.command(runtime), start_new_session=True,
            env=dict(os.environ, PATH='/no-tools', LLVM_PROFILE_FILE=str(ROOT / 'build/public-sdk-%p.profraw')),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup, process)
        deadline = time.monotonic() + 30
        while True:
            self.assertIsNone(process.poll(), 'supervisor ended before release')
            self.assertLess(time.monotonic(), deadline)
            try:
                app = prepared_native.files.read(runtime / 'native-application-start.json')[0]
                break
            except (FileNotFoundError, prepared_native.files.PublicationPending):
                time.sleep(.01)
        self.assertEqual((os.getpgid(app['pid']), os.getsid(app['pid'])), (app['pid'], app['pid']))
        self.assertFalse(select.select([process.stdout], [], [], 0)[0])
        prepared_native.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
        if not plain:
            control.wait_ready(runtime / 'app/control.json', app['pid'])
        return process, runtime, app['pid']

    @staticmethod
    def cleanup(process):
        if process.poll() is None: process.terminate()
        process.communicate(timeout=10)

    def call(self, process, value, expected):
        process.stdin.write(f'{value}\n'.encode()); process.stdin.flush()
        self.assertTrue(select.select([process.stdout], [], [], 10)[0])
        self.assertEqual(process.stdout.readline(), expected)

    def finish(self, process, runtime):
        output, error = process.communicate(timeout=15)
        self.assertEqual((process.returncode, output, error), (0, b'', b''))
        final = prepared_native.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
        self.assertEqual(final['failures'], {})
        self.assertTrue(final['inputs_rechecked'])
        return final['sdk_health']

    def spans(self):
        return [span for request in self.capture.traces for resource in request.resource_spans
                for scope in resource.scope_spans for span in scope.spans]

    def exercise(self, *, recipe=False, cli=False):
        output, built = self.build(recipe=recipe, cli=cli)
        for plain in (True, False):
            process, runtime, pid = self.start(output, 'plain' if plain else 'woven', plain=plain)
            for index, (number, expected) in enumerate(((2, b'7\n'), (-1, b'caught -1\n'),
                                                       (13, b'caught 42\n'), (20, b'20\n'))):
                if not plain:
                    collection, emission = ((False, False), (True, False), (False, True), (True, True))[index]
                    endpoint = runtime / 'app/control.json'
                    control.request(endpoint, 'collection ' + ('on' if collection else 'off'))
                    self.assertEqual(control.request(endpoint, 'emission ' + ('on' if emission else 'off'))['pid'], pid)
                self.call(process, number, expected)
                if not plain:
                    control.request(endpoint, 'flush')
                    if index < 3: self.assertFalse(self.capture.metrics)
            final = self.finish(process, runtime)
            if plain:
                self.assertIsNone(final)
            else:
                self.assertEqual(final['object_accounting']['admitted'], '3')
                self.assertEqual(final['object_accounting']['completed'], '2')
                self.assertEqual(final['object_accounting']['censored'], '1')
                self.assertEqual(final['object_accounting']['lost'], '0')
                self.assertEqual(final['object_accounting']['invalid'], '0')
                self.assertEqual(final['function_accounting']['metadata_errors'], '0')
                self.assertTrue(final['object_accounting']['closed'])
        lifetimes = [span for span in self.spans() if span.name.endswith(' lifetime')]
        self.assertEqual(len(lifetimes), 3)
        self.assertEqual(len([span for span in self.spans() if '::workload' in span.name]), 4)
        self.assertTrue(all(len(span.links) == 1 and not span.parent_span_id for span in lifetimes))
        self.assertTrue(self.capture.metrics)
        native_launch.verify(built)
        self.assertEqual({str(path): native_gate.identity(path) for path in self.root.rglob('*') if path.is_file()},
                         self.original_sources)

    def test_public_source_build_preserves_objects_errors_and_all_live_states(self):
        self.exercise()

    def test_mixed_context_overlay_and_neutral_driver_do_not_add_observer_lifetimes(self):
        self.exercise(recipe=True)

    def test_public_cli_enables_automatic_lifetimes_from_the_common_policy(self):
        self.exercise(cli=True)

    def test_documented_original_example_and_common_policy_produce_lifetime_spans(self):
        data = copy.deepcopy(Policy.load(ROOT / 'examples/native-lifetimes.toml').data)
        data['export']['endpoint'] = self.policy.data['export']['endpoint']
        self.policy = Policy.parse(data)
        source = ROOT / 'examples/native/lifetimes.cpp'
        before = native_gate.identity(source)
        output = self.base / 'compiled'
        native_build.build(self.policy, 'cpp', source.parent, [source], output, sdk_directory=self.sdk)
        for plain in (True, False):
            process, runtime, _ = self.start(output, 'plain' if plain else 'woven', plain=plain)
            self.call(process, 2, b'7\n')
            self.call(process, -1, b'caught -1\n')
            report = self.finish(process, runtime)
            if not plain:
                self.assertEqual(report['object_accounting']['completed'], '1')
                self.assertEqual(report['object_accounting']['lost'], '0')
        self.assertEqual(native_gate.identity(source), before)
        lifetimes = [span for span in self.spans() if span.name == 'demo::Item lifetime']
        self.assertEqual(len(lifetimes), 1)
        self.assertFalse(self.capture.metrics)

    def test_changed_type_selection_headers_and_incomplete_semantics_refuse(self):
        output, built = self.build(recipe=True)
        changed = copy.deepcopy(self.policy.data); changed['lifetimes']['include'] = ['Other']
        with self.assertRaisesRegex(ValueError, 'selection'):
            native_build.verify(output, Policy.parse(changed))
        missing = built.manifest(); missing['object_inventories'].pop('work')
        with self.assertRaisesRegex(ValueError, 'denominator'):
            native_object_build.verify(missing, output)
        header = self.root / 'include/item.hpp'
        self.assertIn(str(header), {row['path'] for row in built.manifest()['dependencies']['plain-work']})
        header.write_text(MODEL + '\n')
        with self.assertRaises(ValueError): native_build.verify(output, self.policy)

    def test_semantic_claim_cannot_be_repaired_by_rehashing_its_inventory(self):
        output, built = self.build()
        value = built.manifest(); record = value['object_inventories']['unit_0']
        filename = Path(record['path']); document = json.loads(filename.read_text())
        self.assertTrue(document['translation_units'][0]['constructions'])
        document['translation_units'][0]['constructions'] = []
        filename.write_text(json.dumps(document, indent=2) + '\n')
        value['object_inventories']['unit_0'] = native_gate.identity(filename)
        with self.assertRaisesRegex(ValueError, 'actual Clang records'):
            native_object_build.verify(value, output)

    def test_global_thread_local_and_member_initialisers_have_real_lifetimes(self):
        text = MODEL + '''demo::Item global_item(5);
thread_local demo::Item tls_item(6);
struct Holder {
 demo::Item member{7};
 Holder(){}
 explicit Holder(int input):member(8){}
 explicit Holder(char input){}
 ~Holder(){}
};
extern "C" int workload(int input){Holder first;Holder second(1);Holder third('x');
 return global_item.value+tls_item.value+first.member.value+second.member.value+third.member.value+input;}
''' + MAIN
        output, _ = self.build(text=text)
        process, runtime, _ = self.start(output, 'woven')
        self.call(process, 2, b'35\n')
        report = self.finish(process, runtime)
        self.assertEqual(report['object_accounting']['admitted'], '5')
        self.assertEqual(report['object_accounting']['completed'], '5')
        self.assertEqual(report['object_accounting']['censored'], '0')
        self.assertEqual(report['object_accounting']['lost'], '0')
        self.assertEqual(report['object_accounting']['invalid'], '0')
        lifetimes = [span for span in self.spans() if span.name.endswith(' lifetime')]
        self.assertEqual(len(lifetimes), 5)
        self.assertEqual(sum(not span.links for span in lifetimes), 1)

    def test_lifetimes_do_not_require_function_selection(self):
        data = copy.deepcopy(self.policy.data); data['functions']['include'] = []
        self.policy = Policy.parse(data)
        output, _ = self.build()
        process, runtime, _ = self.start(output, 'woven')
        self.call(process, 2, b'7\n')
        report = self.finish(process, runtime)
        self.assertEqual(report['function_accounting']['started'], '0')
        self.assertEqual(report['object_accounting']['completed'], '1')
        spans = self.spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, 'demo::Item lifetime')
        self.assertFalse(spans[0].links)

    def test_selected_trivial_aggregate_refuses_instead_of_claiming_observation(self):
        data = copy.deepcopy(self.policy.data); data['lifetimes']['include'] = ['Simple']
        self.policy = Policy.parse(data)
        source = self.root / 'main.cpp'
        source.write_text('struct Simple{int value;};int main(){Simple item{3};return item.value;}\n')
        with self.assertRaisesRegex(ValueError, 'compilation failed'):
            native_build.build(self.policy, 'cpp', self.root, [source], self.base / 'compiled', sdk_directory=self.sdk)
        self.assertFalse((self.base / 'compiled/native-source-build.json').exists())

    def test_type_exclusions_emit_function_traces_without_lifetime_observations(self):
        data = copy.deepcopy(self.policy.data); data['lifetimes']['exclude'] = ['demo::Item']
        self.policy = Policy.parse(data)
        output, _ = self.build()
        process, runtime, _ = self.start(output, 'woven')
        self.call(process, 2, b'7\n')
        report = self.finish(process, runtime)
        self.assertEqual(report['object_accounting']['admitted'], '0')
        self.assertEqual(report['object_accounting']['completed'], '0')
        self.assertEqual(len(self.spans()), 1)
        self.assertIn('::workload', self.spans()[0].name)

    def test_common_limit_and_traces_off_are_applied_to_the_same_public_sdk(self):
        data = copy.deepcopy(self.policy.data)
        data['lifetimes']['max_live'] = 1
        data['traces']['enabled'] = False
        data['metrics'].update(collection=True, emission=True)
        self.policy = Policy.parse(data)
        output, _ = self.build()
        process, runtime, _ = self.start(output, 'woven')
        self.call(process, 20, b'20\n'); self.call(process, 20, b'20\n')
        report = self.finish(process, runtime)
        self.assertEqual(report['object_accounting']['admitted'], '1')
        self.assertEqual(report['object_accounting']['lost'], '1')
        self.assertEqual(report['object_accounting']['censored'], '1')
        self.assertFalse(self.capture.traces)
        self.assertTrue(self.capture.metrics)

