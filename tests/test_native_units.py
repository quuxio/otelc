"""Explicit native contexts, neutral drivers, private overlays and owned execution."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

from otelc import control, native_build, native_gate, native_launch, native_units
from otelc import prepared_native as prepared
from otelc.config import Policy
from test_java_integration import Capture

ROOT = Path(__file__).resolve().parents[1]


def fixture(base: Path, language: str, *, private_overlay: bool = False,
            work_language: str | None = None) -> tuple[Path, dict]:
    """Ordinary application inputs and a separately generated observation caller."""
    root, helpers = base / 'application', base / 'helpers'
    root.mkdir(); helpers.mkdir()
    (root / 'src').mkdir(); (root / 'entry').mkdir(); (root / 'include').mkdir()
    suffix = '.cpp' if language == 'cpp' else '.c'
    work_language = work_language or language
    (root / 'include/work.h').write_text('#ifdef __cplusplus\nextern "C" {\n#endif\nint workload(int);\n#ifdef __cplusplus\n}\n#endif\n')
    work = root / ('src/work' + ('.cpp' if work_language == 'cpp' else '.c'))
    error = '#include <stdexcept>\n' if language == 'cpp' else ''
    work.write_text(('#include <stdexcept>\n' if work_language == 'cpp' else '')
        + '#include "work.h"\n__attribute__((noinline)) int workload(int x){'
        + ('if(x<0)throw std::runtime_error("original");' if work_language == 'cpp' else '')
        + 'return x*FACTOR+1;}\n')
    entry = root / ('entry/main' + suffix)
    entry.write_text('#include <stdio.h>\n#include <stdlib.h>\n#include "work.h"\nint state=OFFSET;\n'
        '__attribute__((constructor)) static void initialise(void){const char*p=getenv("CTOR_MARKER");'
        'if(p){FILE*f=fopen(p,"w");if(f){fputs("ctor\\n",f);fclose(f);}}}\n'
        'int main(void){return workload(state);}\n')
    driver = helpers / ('driver' + suffix)
    caller = ('try{printf("%d\\n",workload(x));}catch(const std::exception&e){printf("%s\\n",e.what());}'
              if language == 'cpp' else 'printf("%d\\n",workload(x));')
    snapshot = 'extern "C" int observed_state(void);\n' if private_overlay else ''
    driver.write_text('#include <stdio.h>\n#include <unistd.h>\n#include "work.h"\n' + error + snapshot
        + 'int main(int argc,char**argv){printf("ready %ld %s",(long)getpid(),argc>1?argv[1]:"none");'
        + ('printf(" %d",observed_state());' if private_overlay else '')
        + 'puts("");fflush(stdout);int x;while(scanf("%d",&x)==1){' + caller + 'fflush(stdout);}return 0;}\n')
    assets, overlay = [str(driver)], None
    if private_overlay:
        augmented = helpers / 'private.cpp'
        augmented.write_bytes(entry.read_bytes() + b'extern "C" __attribute__((no_profile_instrument_function)) int observed_state(void){return state;}\n')
        overlay = helpers / 'overlay.json'
        overlay.write_text(json.dumps({'version': 0, 'use-external-names': False, 'roots': [
            {'type': 'file', 'name': str(entry), 'external-contents': str(augmented)}]}) + '\n')
        assets += [str(augmented), str(overlay)]
    def unit(identifier, role, source, directory, flags, overlay=None, rename=False):
        return {'id': identifier, 'role': role, 'source': str(source), 'language': language,
                'directory': str(directory), 'flags': flags, 'overlay': str(overlay) if overlay else None,
                'rename_main': rename}
    recipe = {'schema_version': 1, 'kind': native_units.RECIPE_KIND, 'assets': assets,
        'units': [unit('work', 'application', work, root / 'src',
                      ['-std=c++20' if work_language == 'cpp' else '-std=c11', '-O1', '-D', 'FACTOR=3', '-I', '../include']),
                  unit('entry', 'application', entry, root / 'entry',
                      ['-std=c++17' if language == 'cpp' else '-std=c17', '-O0', '-D', 'OFFSET=5', '-I', '../include'], overlay, True),
                  unit('observer', 'driver', driver, helpers,
                      ['-O1', '-I', str(root / 'include')])],
        'link': {'language': language, 'directory': str(root), 'flags': ['-pthread']}}
    recipe['units'][0]['language'] = work_language
    return root, recipe


class NativeUnitRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root, self.recipe = fixture(self.base, 'cpp', private_overlay=True)

    def test_explicit_descriptor_and_matched_commands_do_not_probe_the_driver(self) -> None:
        self.assertEqual(prepared.protocols()['build'], 2)
        self.assertEqual(prepared.protocols(build_version=3)['unit_recipe'], 1)
        for version in (True, 1, 4):
            with self.assertRaises(ValueError): prepared.protocols(build_version=version)
        descriptor = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', '/no-policy',
            'native-protocols', '--build-version', '3'], env=dict(os.environ, PATH='/no-tools',
                OTELC_PYTHON=sys.executable), capture_output=True, check=True, timeout=10)
        self.assertEqual(json.loads(descriptor.stdout), prepared.protocols(build_version=3))
        recipe = native_units.validate(self.recipe, self.root)
        recipe['units'][0]['flags'].append('-g')
        self.assertNotEqual(recipe, self.recipe)
        commands = native_units.steps(self.recipe, self.base / 'out', {'directory': '/llvm22'}, '/plugin', Path('/sdk.so'))
        units = [row for row in commands if 'unit' in row]
        self.assertEqual(len(units), 6)
        for row in units:
            present = '-fpass-plugin=/plugin' in row['arguments']
            self.assertEqual(present, row['lane'] == 'instrumented' and row['unit'] != 'observer')
        self.assertEqual([row['directory'] for row in units[:3]], [unit['directory'] for unit in self.recipe['units']])
        self.assertEqual(sum('--redefine-sym' in row['arguments'] for row in commands), 2)

    def test_unbound_private_helpers_and_unsupported_contexts_fail_before_compiling(self) -> None:
        for kind in ('schema', 'fields', 'repeat', 'role', 'suffix', 'flag', 'missingvalue', 'path', 'asset', 'driver', 'link', 'reserved'):
            recipe = copy.deepcopy(self.recipe)
            unit = recipe['units'][0]
            if kind == 'schema': recipe['schema_version'] = True
            elif kind == 'fields': unit['extra'] = 1
            elif kind == 'repeat': recipe['units'].append(copy.deepcopy(unit))
            elif kind == 'role': unit['role'] = 'foreign'
            elif kind == 'suffix': unit['language'] = 'c'
            elif kind == 'flag': unit['flags'] += ['-fpass-plugin=/foreign']
            elif kind == 'missingvalue': unit['flags'] += ['-D']
            elif kind == 'path': unit['flags'] += ['-include', '../include']
            elif kind == 'asset': recipe['assets'].append(recipe['assets'][0])
            elif kind == 'driver': recipe['units'][-1]['source'] = unit['source']
            elif kind == 'link': recipe['link']['flags'] = ['@response']
            else: unit['id'] = 'link'
            with self.subTest(kind=kind), self.assertRaises(ValueError): native_units.validate(recipe, self.root)

    def test_overlay_refuses_aliases_unknown_or_missing_private_inputs(self) -> None:
        overlay = Path(self.recipe['units'][1]['overlay'])
        original = json.loads(overlay.read_text())
        for kind in ('schema', 'externalnames', 'unknown', 'escape', 'unbound', 'repeat'):
            document = copy.deepcopy(original)
            if kind == 'schema': document['version'] = 1
            elif kind == 'externalnames': document['use-external-names'] = True
            elif kind == 'unknown': document['roots'][0]['extra'] = True
            elif kind == 'escape': document['roots'][0]['name'] = self.recipe['assets'][0]
            elif kind == 'unbound': document['roots'][0]['external-contents'] = self.recipe['units'][0]['source']
            else: document['roots'].append(copy.deepcopy(document['roots'][0]))
            overlay.write_text(json.dumps(document))
            with self.subTest(kind=kind), self.assertRaises(ValueError): native_units.validate(self.recipe, self.root)


class NativeUnitExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-units-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.sdk = Path(os.environ['OTELC_NATIVE_SDK_BUILD']) if os.environ.get('OTELC_NATIVE_SDK_BUILD') else cls.base / 'sdk'
        if not os.environ.get('OTELC_NATIVE_SDK_BUILD'):
            native_launch.build_sdk(cls.sdk)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.base = type(self).base / self._testMethodName
        self.base.mkdir()
        self.capture = Capture()
        self.addCleanup(self.capture.close)
        self.policy = Policy.parse({'schema_version': 3, 'languages': ['c', 'cpp'],
            'sources': {'include': ['**']}, 'functions': {'include': ['**']},
            'metrics': {'collection': False, 'emission': False},
            'traces': {'enabled': True, 'sample_ratio': 1.0},
            'export': {'endpoint': f'http://127.0.0.1:{self.capture.server.server_port}',
                       'interval_ms': 60000, 'timeout_ms': 1000, 'shutdown_timeout_ms': 5000}})

    def build(self, language: str, *, work_language: str | None = None, cli: bool = False):
        root, recipe = fixture(self.base, language, private_overlay=language == 'cpp', work_language=work_language)
        before = {str(path): native_gate.identity(path) for path in root.rglob('*') if path.is_file()}
        output = self.base / 'compiled'
        if cli:
            recipe_path, config = self.base / 'recipe.json', self.base / 'otelc.toml'
            recipe_path.write_text(json.dumps(recipe))
            config.write_text('schema_version=3\nlanguages=["c","cpp"]\n'
                '[sources]\ninclude=["**"]\n[functions]\ninclude=["**"]\n')
            result = subprocess.run([str(ROOT / 'bin/quux-otelc'), '--config', str(config), 'native-build',
                '--language', language, '--root', str(root), '--recipe', str(recipe_path),
                '--output', str(output), '--sdk-build', str(self.sdk)],
                env=dict(os.environ, OTELC_PYTHON=sys.executable), capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
        else:
            value = native_build.build(self.policy, language, root, [], output, sdk_directory=self.sdk, recipe=recipe)
        self.assertEqual(value['schema_version'], 3)
        built, _, _ = native_build.verify(output, self.policy)
        return root, recipe, output, built, before

    def wait_file(self, path: Path, process: subprocess.Popen) -> dict:
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                self.fail('supervisor ended before its gate: ' + str(process.communicate()))
            self.assertLess(time.monotonic(), deadline)
            try:
                return prepared.files.read(path)[0]
            except (FileNotFoundError, prepared.files.PublicationPending):
                time.sleep(.005)

    def line(self, process: subprocess.Popen) -> bytes:
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'application response timed out')
        return process.stdout.readline()

    @staticmethod
    def cleanup(process: subprocess.Popen) -> None:
        if process.poll() is None: process.terminate()
        process.communicate(timeout=10)

    def exercise(self, language: str, *, work_language: str | None = None) -> None:
        root, recipe, output, built, before = self.build(language, work_language=work_language)
        exceptions = (work_language or language) == 'cpp'
        for plain in (True, False):
            runtime = self.base / ('plain' if plain else 'instrumented')
            marker = self.base / ('marker-' + str(plain))
            prepared.prepare(self.policy, runtime, build_directory=output, plain=plain, arguments=('literal $value; *',))
            process = subprocess.Popen(prepared.command(runtime),
                env=dict(os.environ, CTOR_MARKER=str(marker), PATH='/no-tools'),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self.addCleanup(self.cleanup, process)
            app = self.wait_file(runtime / 'native-application-start.json', process)
            parent = subprocess.check_output(['/bin/ps', '-p', str(app['pid']), '-o', 'ppid='], text=True, timeout=5)
            self.assertEqual(int(parent), process.pid)
            self.assertEqual((os.getpgid(app['pid']), os.getsid(app['pid'])), (app['pid'], app['pid']))
            self.assertEqual(app['parent_pid'], process.pid)
            self.assertFalse(marker.exists())
            prepared.release(runtime, pid=app['pid'], supervisor_pid=process.pid)
            ready = self.line(process)
            self.assertEqual(ready, f'ready {app["pid"]} literal $value; *{(" 5" if language == "cpp" else "")}\n'.encode())
            self.assertEqual(marker.read_text(), 'ctor\n')
            states = ((False, False), (True, False), (False, True), (True, True))
            for index, (number, flags) in enumerate(zip((2, -1, 3, -2), states, strict=True)):
                if not plain:
                    endpoint = runtime / 'app/control.json'
                    control.wait_ready(endpoint, app['pid'])
                    control.request(endpoint, 'collection ' + ('on' if flags[0] else 'off'))
                    state = control.request(endpoint, 'emission ' + ('on' if flags[1] else 'off'))
                    self.assertEqual(state['pid'], app['pid'])
                process.stdin.write(f'{number}\n'.encode()); process.stdin.flush()
                self.assertEqual(self.line(process), b'original\n' if exceptions and number < 0 else f'{number*3+1}\n'.encode())
                if not plain:
                    control.request(endpoint, 'flush')
                    if index < 3: self.assertEqual(self.capture.points('otelc.function.calls'), [])
            process.stdin.close(); process.stdin = None
            out, err = process.communicate(timeout=10)
            self.assertEqual((process.returncode, out, err), (0, b'', b''))
            final = prepared.execution(runtime, supervisor_pid=process.pid, parent_pid=os.getpid(), returncode=0)
            self.assertEqual(final['failures'], {})
            if plain: self.assertIsNone(final['sdk_health'])
            else:
                observed = [span.name for request in self.capture.traces for resource in request.resource_spans
                            for scope in resource.scope_spans for span in scope.spans]
                self.assertEqual(final['sdk_health']['function_accounting']['ended'], '5', observed)
        spans = [span for request in self.capture.traces for resource in request.resource_spans
                 for scope in resource.scope_spans for span in scope.spans]
        self.assertEqual(len(spans), 5)
        self.assertEqual(sum('::workload' in span.name for span in spans), 4)
        self.assertFalse(any('driver' in span.name or 'observed_state' in span.name for span in spans))
        points = self.capture.points('otelc.function.calls')
        self.assertTrue(points)
        self.assertTrue(all(point.as_int == 2 for point in points))
        if exceptions:
            errors = self.capture.points('otelc.function.errors')
            self.assertTrue(errors)
            self.assertTrue(all(point.as_int == 2 for point in errors))
        self.assertEqual({str(path): native_gate.identity(path) for path in root.rglob('*') if path.is_file()}, before)
        self.assertEqual(native_launch.verify(built)['recipe'], recipe)

    def test_c_per_unit_context_and_neutral_driver_use_both_owned_lanes(self) -> None:
        self.exercise('c')

    def test_cpp_private_overlay_preserves_exceptions_and_excludes_tooling(self) -> None:
        self.exercise('cpp')

    def test_cpp_target_can_compile_a_c_unit_with_its_own_language_context(self) -> None:
        self.exercise('cpp', work_language='c')

    def test_changed_headers_helpers_and_missing_compiler_records_refuse(self) -> None:
        parent = self.base
        for index in range(3):
            with self.subTest(index=index):
                self.base = parent / str(index); self.base.mkdir()
                root, recipe, output, built, _ = self.build('cpp')
                path = (root / 'include/work.h', Path(recipe['assets'][0]), output / 'plain-work.stdout')[index]
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                with self.assertRaises(ValueError): native_launch.verify(built)
                path.write_bytes(original)
                # Original facts stay invalid even if the bytes are restored.
                with self.assertRaises(ValueError): native_launch.verify(built)

    def test_incomplete_or_relabelled_build_records_cannot_hide_inputs_or_commands(self) -> None:
        _, _, _, built, _ = self.build('cpp')
        original = built.manifest()
        for kind in ('input', 'file', 'artifact', 'dependency', 'tool', 'toolpath', 'command', 'malformed'):
            value = copy.deepcopy(original)
            if kind == 'input': value['inputs'].pop()
            elif kind == 'file': value['files'].pop()
            elif kind == 'artifact': value['artifacts'].pop()
            elif kind == 'dependency': value['dependencies'].pop('plain-work')
            elif kind == 'tool': value['tools']['foreign'] = value['tools']['objcopy']
            elif kind == 'toolpath': value['tools']['objcopy']['path'] = '/bin/sh'
            elif kind == 'command': value['commands'][0]['directory'] = '/'
            else: value['files'] = [None]
            with self.subTest(kind=kind), self.assertRaises(ValueError): native_units.verify(built, value)
        self.assertEqual(native_launch.verify(built), original)

    def test_public_cli_accepts_the_explicit_recipe_without_source_arguments(self) -> None:
        _, _, _, built, _ = self.build('c', cli=True)
        self.assertEqual(native_launch.verify(built)['profile'], native_units.PROFILE)
