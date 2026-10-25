"""Owned pre-exec children cannot run application constructors before release."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest

from otelc import llvm_backend, native_gate

ROOT = Path(__file__).resolve().parents[1]


class NativeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        (ROOT / 'build').mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix='native-gate-', dir=ROOT / 'build')
        cls.base = Path(cls.temporary.name).resolve()
        cls.product = native_gate.build(cls.base / 'tool', coverage=True)
        compiler = cls.product.metadata()['compiler']['path']
        source = cls.base / 'app.c'
        source.write_text('''#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
__attribute__((constructor)) static void initialise(void){const char*p=getenv("CTOR_MARKER");if(p){FILE*f=fopen(p,"a");if(f){fputs("ctor\\n",f);fclose(f);}}}
int main(int argc,char**argv){printf("%ld %ld %s\\n",(long)getpid(),(long)getpgrp(),argc>1?argv[1]:"none");fflush(stdout);int x;if(scanf("%d",&x)==1)printf("%d\\n",x+7);return 0;}
''')
        cls.application = cls.base / 'application'
        subprocess.run([compiler, '-std=c11', str(source), '-o', str(cls.application)], check=True, capture_output=True)
        library = cls.base / 'load.c'
        library.write_text('''#include <stdio.h>
#include <stdlib.h>
__attribute__((constructor)) static void initialise(void){const char*p=getenv("LOADER_MARKER");if(p){FILE*f=fopen(p,"a");if(f){fputs("loaded\\n",f);fclose(f);}}}
''')
        cls.library = cls.base / ('load.dylib' if sys.platform == 'darwin' else 'load.so')
        subprocess.run([compiler, '-dynamiclib' if sys.platform == 'darwin' else '-shared', '-fPIC',
                        str(library), '-o', str(cls.library)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def waiting(self, name: str, **options) -> native_gate.Waiting:
        marker = self.base / (name + '.ctor')
        environment = dict(os.environ, CTOR_MARKER=str(marker)) | options.pop('environment', {})
        waiting = native_gate.start(self.product, (str(self.application), 'literal $value; *'),
            cwd=self.base, environment=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        self.addCleanup(self.cleanup, waiting)
        return waiting

    @staticmethod
    def cleanup(waiting: native_gate.Waiting) -> None:
        waiting.cancel()
        waiting.process.communicate(timeout=5)

    def test_constructor_streams_and_process_identity_are_preserved_across_exec(self) -> None:
        waiting = self.waiting('normal')
        value = waiting.ready()
        self.assertEqual(value['pid'], waiting.process.pid)
        self.assertEqual(value['parent_pid'], os.getpid())
        self.assertFalse((self.base / 'normal.ctor').exists())
        self.assertFalse(select.select([waiting.process.stdout], [], [], 0)[0])
        with self.assertRaisesRegex(ValueError, 'before release'):
            self.waiting('unverified').release()
        waiting.release()
        output, errors = waiting.process.communicate(b'12\n', timeout=5)
        self.assertEqual((waiting.process.returncode, errors), (0, b''))
        self.assertEqual(output, f'{value["pid"]} {value["process_group"]} literal $value; *\n19\n'.encode())
        self.assertEqual((self.base / 'normal.ctor').read_text(), 'ctor\n')
        with self.assertRaises(ValueError):
            waiting.release()

    def test_existing_loader_library_runs_only_in_the_released_image(self) -> None:
        marker = self.base / 'loader.marker'
        key = 'DYLD_INSERT_LIBRARIES' if sys.platform == 'darwin' else 'LD_PRELOAD'
        waiting = self.waiting('loader', environment={key: str(self.library), 'LOADER_MARKER': str(marker)})
        waiting.ready()
        self.assertFalse(marker.exists(), 'application library constructor ran inside the gate')
        waiting.release()
        output, errors = waiting.process.communicate(b'1\n', timeout=5)
        self.assertEqual((waiting.process.returncode, errors), (0, b''))
        self.assertTrue(output.endswith(b'8\n'))
        self.assertEqual(marker.read_text(), 'loaded\n')

    def test_cancel_or_timeout_does_not_execute_the_application(self) -> None:
        waiting = self.waiting('cancel')
        waiting.ready()
        waiting.cancel()
        self.assertFalse((self.base / 'cancel.ctor').exists())
        with self.assertRaises(ValueError):
            waiting.release()
        timed = self.waiting('timeout', wait_ms=20)
        self.assertEqual(timed.process.wait(timeout=5), 124)
        self.assertFalse((self.base / 'timeout.ctor').exists())

    def test_changed_target_bytes_or_facts_cannot_be_recertified_before_release(self) -> None:
        waiting = self.waiting('changed')
        waiting.ready()
        original = self.application.read_bytes()
        self.application.write_bytes(original)
        with self.assertRaisesRegex(ValueError, 'target changed'):
            waiting.release()
        self.assertFalse((self.base / 'changed.ctor').exists())

    def test_invalid_or_closed_release_capabilities_never_start_the_target(self) -> None:
        waiting = self.waiting('bad-ack')
        waiting.ready()
        os.write(waiting.release_fd, b'X')
        self.assertEqual(waiting.process.wait(timeout=5), 125)
        self.assertFalse((self.base / 'bad-ack.ctor').exists())
        executable = self.product.metadata()['executable']['path']
        for arguments in ([], ['bad', '4', '10', '5', str(self.application)],
                          ['-1', '4', '10', '5', str(self.application)],
                          ['3', '3', '10', '5', str(self.application)],
                          ['3', '4', '0', '5', str(self.application)],
                          ['3', '4', '300001', '5', str(self.application)],
                          ['3', '4', '10', '5', 'relative'],
                          ['90000', '90001', '10', '90002', str(self.application)]):
            result = subprocess.run([executable, *arguments], env={'LLVM_PROFILE_FILE':
                str(self.base / 'tool/%m-%p.profraw')}, capture_output=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (125, b'', b''))

    def test_invalid_python_requests_are_refused_before_starting_a_child(self) -> None:
        from unittest.mock import patch
        for command, budget in (((), 10), ((str(self.application),), True),
                                 (('relative',), 10), ((str(self.application), '\0'), 10)):
            with patch.object(subprocess, 'Popen', side_effect=AssertionError('no child')), \
                    self.assertRaises(ValueError):
                native_gate.start(self.product, command, cwd=self.base, wait_ms=budget)
        for environment in ({'bad=value': 'x'}, {'': 'x'}, {'a': '\0'}, {'a': 1},
                            {'a': 'x' * (4 * 1024 * 1024)}):
            with patch.object(subprocess, 'Popen', side_effect=AssertionError('no child')), \
                    self.assertRaises(ValueError):
                native_gate.start(self.product, (str(self.application),), cwd=self.base, environment=environment)

    def test_empty_environment_preserves_literal_arguments_and_application_identity(self) -> None:
        waiting = native_gate.start(self.product, (str(self.application), 'literal $value; *'),
            cwd=self.base, environment={}, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.cleanup, waiting)
        waiting.ready()
        waiting.release()
        output, error = waiting.process.communicate(b'', timeout=5)
        self.assertEqual(waiting.process.returncode, 0)
        self.assertEqual(error, b'')
        self.assertEqual(output, f'{waiting.process.pid} {waiting.process.pid} literal $value; *\n'.encode())

    def test_failed_exec_with_empty_or_nonempty_environment_retains_errno_and_exit(self) -> None:
        target = self.base / 'nonexecutable'
        target.write_bytes(b'not an executable image')
        target.chmod(0o600)
        for environment in ({}, {'LITERAL': '$value; *'}):
            with self.subTest(empty=not environment):
                waiting = native_gate.start(self.product, (str(target),), cwd=self.base,
                    environment=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.addCleanup(self.cleanup, waiting)
                waiting.ready()
                waiting.release()
                output, error = waiting.process.communicate(timeout=5)
                self.assertEqual(waiting.process.returncode, 127)
                self.assertEqual(output, b'')
                self.assertEqual(error, b'otelc native exec gate: Permission denied\n')

    def test_z_actual_gate_source_has_at_least_eighty_percent_native_coverage(self) -> None:
        self.product.verify()
        metadata = self.product.metadata()
        tools = Path(metadata['compiler']['path']).parent
        profiles = sorted((self.base / 'tool').glob('*.profraw'))
        self.assertTrue(profiles)
        merged = self.base / 'gate.profdata'
        subprocess.run([str(tools / 'llvm-profdata'), 'merge', '-sparse', *map(str, profiles),
                        '-o', str(merged)], check=True, capture_output=True)
        command = [str(tools / 'llvm-cov'), 'export', metadata['executable']['path'],
                   '-instr-profile=' + str(merged), str(native_gate.SOURCE)]
        value = json.loads(subprocess.check_output(command))
        records = [row for block in value['data'] for row in block['files']
                   if Path(row['filename']) == native_gate.SOURCE]
        self.assertEqual(len(records), 1)
        lines = records[0]['summary']['lines']
        self.assertGreaterEqual(lines['percent'], 80)
        output = ROOT / 'build'
        shutil.copyfile(metadata['executable']['path'], output / 'native-gate-test')
        shutil.copyfile(merged, output / 'native-gate.profdata')
        shutil.copyfile(self.base / 'tool/compile_commands.json', output / 'native-gate-compile-commands.json')
        lcov = output / 'native-gate.lcov'
        lcov.write_bytes(subprocess.check_output([*command, '-format=lcov']))
        (output / 'native-gate-coverage.json').write_text(json.dumps({
            'source': llvm_backend.identity(native_gate.SOURCE), 'source_facts': metadata['source'],
            'product': llvm_backend.identity(output / 'native-gate-test'),
            'compiler': metadata['compiler'], 'line_coverage': {str(native_gate.SOURCE): lines},
            'lcov': llvm_backend.identity(lcov),
            'compilation_database': llvm_backend.identity(output / 'native-gate-compile-commands.json'),
            'scope': 'Pre-exec host gate component; not the public native execution profile'}, indent=2) + '\n')
