"""Unedited C allocation lifetimes through public builds and actual SDK OTLP."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from otelc import control, health, native_build, native_gate, native_launch, native_resource_build, prepared_native
from otelc.config import Policy
from test_java_integration import Capture
import test_native_public_lifetimes as _hosts

ROOT = Path(__file__).resolve().parents[1]
APP = '''#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <errno.h>
typedef struct WorkItem {int value;} WorkItem;
static WorkItem*held;
__attribute__((noinline)) int workload(int value){
 if(value==20){held=malloc(sizeof *held);if(!held)return -1;held->value=value;return value;}
 if(value==21){free((void*)held);held=NULL;return value;}
 if(value==30){volatile size_t huge=SIZE_MAX;WorkItem*volatile failed=calloc(huge,sizeof *failed);
   return failed?-30:(errno==ENOMEM?30:-31);}
 if(value==31){WorkItem*volatile zero=calloc(1,sizeof *zero);if(!zero)return -1;
   int result=zero->value;free(zero);return result==0?31:-33;}
 WorkItem*volatile item=malloc(sizeof *item);if(!item)return -1;
 item->value=value*3+1;int result=item->value;
 errno=E2BIG;free((void*)item);if(errno!=E2BIG)return -32;
 char*volatile other=malloc(2);free(other);return result;
}
int main(void){int value;while(scanf("%d",&value)==1){printf("%d\\n",workload(value));fflush(stdout);}return 0;}
'''


class PublicResourceTests(unittest.TestCase):
    start = _hosts.PublicLifetimeTests.start
    call = _hosts.PublicLifetimeTests.call
    finish = _hosts.PublicLifetimeTests.finish
    spans = _hosts.PublicLifetimeTests.spans
    cleanup = staticmethod(_hosts.PublicLifetimeTests.cleanup)

    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.keep = os.environ.get('OTELC_CPP_ANALYSIS') == '1'
        cls.temporary = tempfile.TemporaryDirectory(prefix='public-resource-tests-', dir=ROOT / 'build', delete=not cls.keep)
        cls.directory = Path(cls.temporary.name).resolve()
        cls.sdk = Path(os.environ.get('OTELC_PUBLIC_RESOURCE_SDK', str(cls.directory / 'sdk')))
        if not os.environ.get('OTELC_PUBLIC_RESOURCE_SDK'):
            native_launch.build_sdk(cls.sdk)

    @classmethod
    def tearDownClass(cls) -> None:
        if not cls.keep:
            cls.temporary.cleanup()

    def setUp(self) -> None:
        self.base = type(self).directory / self._testMethodName
        self.base.mkdir()
        self.root = self.base / 'application'; self.root.mkdir()
        self.source = self.root / 'main.c'; self.source.write_text(APP)
        self.capture = Capture(); self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['c'],
            'sources': {'include': ['**']}, 'functions': {'include': ['workload']},
            'lifetimes': {'enabled': True, 'include': ['WorkItem']},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000}})

    def build(self):
        self.original = native_gate.identity(self.source)
        self.compiled = self.base / 'compiled'
        result = native_build.build(self.policy, 'c', self.root, [self.source], self.compiled,
                                    sdk_directory=self.sdk)
        self.assertEqual(result['schema_version'], 5)
        self.assertEqual(native_gate.identity(self.source), self.original)
        return native_build.verify(self.compiled, self.policy)[0]

    def test_public_source_build_same_pid_controls_censoring_and_decoded_lifetimes(self):
        built = self.build()
        plain, plain_runtime, _ = self.start(self.compiled, 'plain', plain=True)
        self.addCleanup(self.cleanup, plain)
        for value, expected in ((1, b'4\n'), (30, b'30\n'), (20, b'20\n'), (21, b'21\n')):
            self.call(plain, value, expected)
        self.assertIsNone(self.finish(plain, plain_runtime))
        process, runtime, pid = self.start(self.compiled, 'instrumented')
        self.addCleanup(self.cleanup, process)
        socket = runtime / 'app/control.json'
        for index, (collection, emission) in enumerate(((False, False), (False, True), (True, False), (True, True)), 1):
            control.request(socket, 'collection ' + ('on' if collection else 'off'))
            control.request(socket, 'emission ' + ('on' if emission else 'off'))
            self.assertEqual(control.request(socket, 'status')['pid'], pid)
            self.call(process, index, f'{index*3+1}\n'.encode())
            control.request(socket, 'flush')
            if index < 4:
                self.assertFalse(self.capture.metrics)
        self.call(process, 30, b'30\n')
        self.call(process, 20, b'20\n')
        final = self.finish(process, runtime)
        counts = final['resource_accounting']
        self.assertEqual({name: counts[name] for name in ('admitted', 'completed', 'censored', 'lost', 'invalid')},
                         {'admitted': '5', 'completed': '4', 'censored': '1', 'lost': '0', 'invalid': '0'})
        self.assertTrue(counts['closed'])
        self.assertNotIn('object_accounting', final)
        lifetimes = [span for span in self.spans() if span.name == 'WorkItem lifetime']
        self.assertEqual(len(lifetimes), 5)
        boundaries = [{item.key: item.value.string_value for item in span.attributes}['boundary'] for span in lifetimes]
        self.assertEqual(boundaries.count('free_entry'), 4)
        self.assertEqual(boundaries.count('shutdown_censored'), 1)
        for span in lifetimes:
            attrs = {item.key: item.value.string_value for item in span.attributes}
            self.assertIn(attrs['boundary'], ('free_entry', 'shutdown_censored'))
            self.assertFalse(any('destructor' in key for key in attrs))
            self.assertEqual(len(span.links), 1)
        self.assertTrue(self.capture.metrics)
        self.assertEqual(native_gate.identity(self.source), self.original)

    def test_resource_inventory_and_emitted_site_tampering_refuse(self):
        built = self.build()
        original = json.loads(built.encoded)
        directory = self.compiled / 'resources/unit_0'
        resolution = directory / 'resolution.json'
        before = resolution.read_bytes()
        value = json.loads(before); value['sites'][0]['emitted_calls'] = True
        resolution.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            native_build.verify(self.compiled, self.policy)
        resolution.write_bytes(before)
        altered = copy.deepcopy(original)
        altered['resource_inventories'] = {}
        with self.assertRaisesRegex(ValueError, 'unit denominator'):
            native_resource_build.verify(altered, self.compiled)

    def interposed_build(self, name, definition, *, driver=False):
        directory = self.base / 'helpers' if driver else self.root
        directory.mkdir(exist_ok=True)
        source = directory / ('override.cpp' if driver else 'override.c')
        source.write_text('#include <stddef.h>\n' + definition + '\n')
        def unit(identifier, role, path, language):
            return {'id': identifier, 'role': role, 'source': str(path), 'language': language,
                    'directory': str(path.parent), 'flags': ['-O2', '-std=c++17' if language == 'cpp' else '-std=c11'],
                    'overlay': None, 'rename_main': False}
        recipe = {'schema_version': 1, 'kind': 'otelc.native.unit-recipe',
            'assets': [str(source)] if driver else [],
            'units': [unit('main', 'application', self.source, 'c'),
                      unit('override', 'driver' if driver else 'application', source, 'cpp' if driver else 'c')],
            'link': {'language': 'cpp' if driver else 'c', 'directory': str(self.root), 'flags': ['-pthread']}}
        return native_build.build(self.policy, 'c', self.root, [], self.base / ('compiled-' + name),
                                   sdk_directory=self.sdk, recipe=recipe)

    def test_another_unit_cannot_replace_the_selected_standard_free(self):
        with self.assertRaisesRegex(ValueError, 'interposed'):
            self.interposed_build('free', 'void free(void*p){(void)p;}')

    def test_allocator_weak_and_neutral_driver_definitions_refuse(self):
        cases = [('malloc', 'void*malloc(size_t n){(void)n;return 0;}'),
                 ('calloc', 'void*calloc(size_t n,size_t m){(void)n;(void)m;return 0;}'),
                 ('realloc', 'void*realloc(void*p,size_t n){(void)n;return p;}'),
                 ('weak', '__attribute__((weak)) void free(void*p){(void)p;}')]
        for name, definition in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'interposed'):
                self.interposed_build(name, definition)
        with self.assertRaisesRegex(ValueError, 'interposed'):
            self.interposed_build('driver', 'extern "C" void free(void*p){(void)p;}', driver=True)

    def test_each_startup_state_retains_traces_and_can_enable_metrics(self):
        self.build()
        for index, (collection, emission) in enumerate(((False, False), (False, True), (True, False), (True, True))):
            data = copy.deepcopy(self.policy.data)
            data['metrics'].update(collection=collection, emission=emission)
            self.policy = Policy.parse(data)
            process, runtime, pid = self.start(self.compiled, 'startup-' + str(index))
            socket = runtime / 'app/control.json'
            initial = control.request(socket, 'status')
            self.assertEqual((initial['pid'], initial['collection'], initial['emission']), (pid, collection, emission))
            self.call(process, 1, b'4\n')
            control.request(socket, 'on')
            self.assertEqual(control.request(socket, 'status')['pid'], pid)
            self.call(process, 2, b'7\n')
            final = self.finish(process, runtime)
            self.assertEqual(final['resource_accounting']['completed'], '2')
            self.assertEqual(final['resource_accounting']['lost'], '0')
        lifetimes = [span for span in self.spans() if span.name == 'WorkItem lifetime']
        self.assertEqual(len(lifetimes), 8)
        self.assertTrue(self.capture.metrics)

    def test_public_cli_build_and_both_lanes_require_no_source_changes(self):
        config = self.base / 'policy.toml'
        config.write_text('schema_version=3\nlanguages=["c"]\n[sources]\ninclude=["**"]\n'
            '[functions]\ninclude=["workload"]\n[lifetimes]\nenabled=true\ninclude=["WorkItem"]\n'
            '[metrics]\ncollection=true\nemission=true\n[traces]\nenabled=true\nsample_ratio=1\n'
            f'[export]\nendpoint="http://127.0.0.1:{self.capture.server.server_port}"\n'
            'interval_ms=60000\ntimeout_ms=1000\nshutdown_timeout_ms=5000\n')
        self.policy = Policy.load(config)
        original = native_gate.identity(self.source)
        output = self.base / 'compiled'
        environment = dict(os.environ, OTELC_PYTHON=os.sys.executable,
                           LLVM_PROFILE_FILE=os.environ.get('LLVM_PROFILE_FILE',
                               str(ROOT / 'build/public-resource-cli-%p.profraw')))
        prefix = [str(ROOT / 'bin/quux-otelc'), '--config', str(config)]
        built = subprocess.run([*prefix, 'native-build', '--language', 'c', '--root', str(self.root),
            '--source', str(self.source), '--output', str(output), '--sdk-build', str(self.sdk)],
            env=environment, capture_output=True, timeout=180)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertEqual(json.loads(built.stdout)['schema_version'], 5)
        for plain in (True, False):
            runtime = self.base / ('plain' if plain else 'woven')
            command = [*prefix, 'native-run', '--build', str(output), '--runtime', str(runtime)]
            if plain:
                command.append('--plain')
            result = subprocess.run(command, input=b'1\n30\n31\n20\n21\n', env=environment,
                                    capture_output=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'4\n30\n31\n20\n21\n', b''))
            report = json.loads((runtime / 'native-execution.json').read_text())['sdk_health']
            if plain:
                self.assertIsNone(report)
                self.assertFalse(self.capture.traces)
            else:
                self.assertEqual(report['resource_accounting']['admitted'], '3')
                self.assertEqual(report['resource_accounting']['completed'], '3')
                self.assertEqual(report['resource_accounting']['lost'], '0')
                self.assertEqual(report['resource_accounting']['censored'], '0')
        self.assertEqual(native_gate.identity(self.source), original)

    def test_per_unit_headers_overlay_and_cpp_driver_keep_only_application_lifetimes(self):
        (self.root / 'src').mkdir(); (self.root / 'include').mkdir()
        header = self.root / 'include/item.h'
        header.write_text('typedef struct WorkItem {int value;} WorkItem;\n')
        work = self.root / 'src/work.c'
        body, main = APP.split('int main(void)', 1)
        work.write_text('#include "item.h"\n' + body.replace('typedef struct WorkItem {int value;} WorkItem;\n', ''))
        self.source.write_text('#include <stdio.h>\nint workload(int);\nint main(void)' + main)
        helpers = self.base / 'helpers'; helpers.mkdir()
        overlay_source = helpers / 'work.c'
        overlay_source.write_bytes(work.read_bytes() + b'__attribute__((no_profile_instrument_function)) '
            b'int observed(void){WorkItem*p=malloc(sizeof *p);if(!p)return -1;p->value=4;int x=p->value;free(p);return x;}\n')
        overlay = helpers / 'overlay.json'
        overlay.write_text(json.dumps({'version': 0, 'use-external-names': False, 'roots': [
            {'type': 'file', 'name': str(work), 'external-contents': str(overlay_source)}]}) + '\n')
        driver = helpers / 'driver.cpp'
        driver.write_text('extern "C" int __oteleq_original_main();extern "C" int observed();'
                          'int main(){return observed()==4?__oteleq_original_main():91;}\n')
        def unit(name, role, source, language, directory, flags, *, rename=False, vfs=None):
            return {'id': name, 'role': role, 'source': str(source), 'language': language,
                    'directory': str(directory), 'flags': flags, 'rename_main': rename,
                    'overlay': str(vfs) if vfs else None}
        recipe = {'schema_version': 1, 'kind': 'otelc.native.unit-recipe',
            'assets': list(map(str, (overlay_source, overlay, driver))),
            'units': [unit('entry', 'application', self.source, 'c', self.root, ['-std=c11', '-O0'], rename=True),
                unit('work', 'application', work, 'c', work.parent, ['-std=c17', '-O1', '-isystem', '../include'], vfs=overlay),
                unit('driver', 'driver', driver, 'cpp', helpers, ['-std=c++17', '-O2'])],
            'link': {'language': 'cpp', 'directory': str(self.root), 'flags': ['-pthread']}}
        before = {str(path): native_gate.identity(path) for path in (self.source, work, header, overlay_source, overlay, driver)}
        output = self.base / 'compiled'
        native_build.build(self.policy, 'c', self.root, [], output, sdk_directory=self.sdk, recipe=recipe)
        process, runtime, _ = self.start(output, 'woven')
        self.call(process, 10, b'31\n')
        final = self.finish(process, runtime)
        self.assertEqual(final['resource_accounting']['admitted'], '1')
        self.assertEqual(final['resource_accounting']['completed'], '1')
        self.assertEqual(final['resource_accounting']['lost'], '0')
        self.assertEqual({str(path): native_gate.identity(path) for path in (self.source, work, header, overlay_source, overlay, driver)}, before)


class ResourceHealthTests(unittest.TestCase):
    def test_resource_health_is_distinct_from_cpp_destruction(self):
        from test_health import report
        value = report()
        counters = ('types', 'admitted', 'completed', 'live', 'releasing', 'ending',
                    'censored', 'lost', 'duplicates', 'invalid')
        value.update(language='c', source_complete=False,
            function_accounting={**dict.fromkeys(('registered', 'started', 'ended', 'active',
                'lost', 'invalid', 'metadata_errors'), '0'), 'closed': True},
            resource_accounting={**dict.fromkeys(counters, '0'), 'closing': True, 'closed': True})
        self.assertIs(health.validate(value), value)
        for field, bad in (('admitted', True), ('releasing', '-1'), ('closed', 1), ('destroying', '0')):
            changed = copy.deepcopy(value); changed['resource_accounting'][field] = bad
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'resource accounting'):
                health.validate(changed)
        changed = copy.deepcopy(value); changed['language'] = 'cpp'
        with self.assertRaisesRegex(ValueError, 'resource accounting'):
            health.validate(changed)


if __name__ == '__main__':
    unittest.main()
