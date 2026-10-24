"""Original applications keep native output while exact entry bytes are retained."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ci import build_python_inputs as tooling

ROOT = Path(__file__).resolve().parents[1]


class NativeInputCase(unittest.TestCase):
    """Build and run actual native inputs for byte-channel and provenance tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = tooling.build(coverage=bool(os.environ.get('OTELC_NATIVE_COVERAGE')))
        cls.argv0 = subprocess.check_output([sys.executable, '-I', '-S', '-c',
            'import sys;print(sys.orig_argv[0])'], text=True).strip()
        output = ROOT / 'build/native'
        output.mkdir(parents=True, exist_ok=True)
        cls.fixture = output / ('python-input-fixture-' + sys.implementation.cache_tag)
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            candidate = Path(temporary) / 'fixture'
            flags = ['-fprofile-instr-generate', '-fcoverage-mapping'] if os.environ.get('OTELC_NATIVE_COVERAGE') else []
            subprocess.run([tooling.compiler(), '-std=c11', '-Wall', '-Wextra', '-Werror', '-O0', '-g',
                '-pthread', *flags, '-I' + str(ROOT / 'native/python'),
                str(ROOT / 'native/python/InputJournal.c'), str(ROOT / 'tests/fixtures/python_input_journal.c'),
                '-o', str(candidate)], check=True)
            os.replace(candidate, cls.fixture)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.number = 0

    def launch(self, arguments: list[str], *, environment: dict | None = None,
               origin_observer: bool = False, entry_origins: bool = False, loader_origins: bool = False,
               origin_root: Path | None = None
               ) -> tuple[int, bytes, bytes, Path, int]:
        self.number += 1
        path = self.root / f'input-{self.number}.journal'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            options = []
            if origin_root is not None:
                options = ['--origin-loader-root', str(origin_root)]
            elif loader_origins:
                options = ['--origin-loader']
            elif entry_origins:
                options = ['--origin-entry']
            elif origin_observer:
                options = ['--origin-observer']
            process = subprocess.Popen([str(self.tool), *options, str(descriptor), sys.executable,
                self.argv0, *arguments], cwd=self.root, env=environment, pass_fds=(descriptor,),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        finally:
            os.close(descriptor)
        try:
            out, err = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        return process.returncode, out, err, path, process.pid

