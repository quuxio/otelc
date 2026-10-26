"""Query original typed Rust bodies; never compile or launch an application."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

SOURCE = '''#[cfg(extra)] mod earlier { pub fn unrelated() -> i32 { 99 } }
mod left { pub fn selected() -> i32 { 7 } }
mod right { pub fn selected() -> i32 { 7 } }
#[cfg(not(rebind))] use left::selected as route;
#[cfg(rebind)] use right::selected as route;
struct Left(i32); struct Right(i32);
impl Left { fn answer(&self) -> i32 { self.0 } }
impl Right { fn answer(&self) -> i32 { self.0 } }
#[cfg(not(receiver))] fn make() -> Left { Left(7) }
#[cfg(receiver)] fn make() -> Right { Right(7) }
fn run() -> i32 { route() }
fn method() -> i32 { make().answer() }
const VALUE: i32 = 3;
static STATE: i32 = 5;
const fn constant() -> i32 { 3 }
async fn later() -> i32 { 3 }
macro_rules! body { () => { fn expanded() -> i32 { 3 } } }
body!();
fn nested() -> i32 { let work = || 3; work() }
fn main() {}
'''


class RustBindingCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT']).resolve() / 'body-bindings'
        cls.base.mkdir(mode=0o700, parents=True, exist_ok=False)
        cls.compiler = Path(os.environ['OTELC_RUST_COMPILER']).resolve(strict=True)
        rustc = Path(os.environ['OTELC_RUSTC']).resolve(strict=True)
        cls.sysroot = subprocess.check_output([str(rustc), '--print', 'sysroot'], text=True).strip()

    def setUp(self) -> None:
        self.folder = self.base / self._testMethodName
        self.root = self.folder / 'app'
        self.root.mkdir(mode=0o700, parents=True)
        self.entry = self.root / 'main.rs'
        self.entry.write_text(SOURCE)
        self.digest = hashlib.sha256(self.entry.read_bytes()).hexdigest()
        self.counter = 0

    def tearDown(self) -> None:
        self.assertEqual(hashlib.sha256(self.entry.read_bytes()).hexdigest(), self.digest)

    def inspect(self, *, flags: tuple[str, ...] = (), changes: dict | None = None):
        self.counter += 1
        prefix = self.folder / f'command-{self.counter}'
        output, image = prefix.with_suffix('.inventory.json'), prefix.with_suffix('.application')
        request = {'schema_version': 1, 'mode': 'binding-inspect', 'root': str(self.root),
                   'entry': str(self.entry), 'sources': {str(self.entry): self.digest},
                   'selected': [], 'output': str(output)} | (changes or {})
        filename = prefix.with_suffix('.request.json')
        filename.write_text(json.dumps(request) + '\n')
        argv = [str(self.compiler), str(self.entry), '--crate-name', 'original_bindings',
                '--edition=2024', '--sysroot', self.sysroot, '-Copt-level=2',
                *flags, '-o', str(image)]
        environment = os.environ | {'OTELC_RUST_REQUEST': str(filename),
            'RUSTC_BOOTSTRAP': '1',
            'LLVM_PROFILE_FILE': str(self.base.parent / '%m-%p.profraw')}
        result = subprocess.run(argv, cwd=self.folder, env=environment,
                                capture_output=True, timeout=120)
        prefix.with_suffix('.stdout').write_bytes(result.stdout)
        prefix.with_suffix('.stderr').write_bytes(result.stderr)
        prefix.with_suffix('.result.json').write_text(json.dumps({
            'arguments': argv, 'exit_code': result.returncode}, indent=2) + '\n')
        self.assertFalse(image.exists(), 'body inspection cannot emit an application image')
        return result, output

    def accepted(self, *flags: str) -> dict:
        result, output = self.inspect(flags=flags)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        value = json.loads(output.read_bytes())
        self.assertEqual(value['kind'], 'otelc.rust.body-bindings')
        self.assertEqual(value['algorithm'], 'rustc-1.98.1-stablehash-128-v1')
        self.assertEqual(value['crate_name'], 'original_bindings')
        self.assertEqual(value['sources'], {str(self.entry): self.digest})
        self.assertEqual(value['original_body_count'], len(value['bodies']))
        self.assertEqual(value['helper_exclusions'], [])
        self.assertFalse(value['source_complete'])
        self.assertFalse(value['semantic_complete'])
        for row in value['bodies']:
            self.assertEqual(row['eligible'], row['reason'] is None)
            if row['eligible']:
                for field in ('pre_optimization_mir', 'typeck'):
                    self.assertIsNotNone(re.fullmatch('[a-f0-9]{32}', row['fingerprints'][field]))
            else:
                self.assertIsNone(row['fingerprints'])
        return {row['function']['compiler_name']: row for row in value['bodies']}

    def test_repeat_and_unrelated_definition_index_changes_keep_original_hashes(self) -> None:
        first, repeat, extra = self.accepted(), self.accepted(), self.accepted('--cfg', 'extra')
        self.assertEqual(first, repeat)
        self.assertNotEqual(first['run']['diagnostic_def_index'], extra['run']['diagnostic_def_index'])
        for name in ('run', 'method', 'nested', 'left::selected', 'right::selected'):
            with self.subTest(name=name):
                self.assertEqual(first[name]['fingerprints'], extra[name]['fingerprints'])
        self.assertIn('earlier::unrelated', extra)

    def test_same_spelled_callee_rebinding_changes_both_original_fingerprints(self) -> None:
        first, rebound = self.accepted(), self.accepted('--cfg', 'rebind')
        self.assertEqual(first['run']['function'], rebound['run']['function'])
        for field in ('pre_optimization_mir', 'typeck'):
            self.assertNotEqual(first['run']['fingerprints'][field], rebound['run']['fingerprints'][field])

    def test_receiver_and_method_type_rebinding_changes_both_fingerprints(self) -> None:
        first, rebound = self.accepted(), self.accepted('--cfg', 'receiver')
        self.assertEqual(first['method']['function'], rebound['method']['function'])
        for field in ('pre_optimization_mir', 'typeck'):
            self.assertNotEqual(first['method']['fingerprints'][field], rebound['method']['fingerprints'][field])

    def test_unavailable_original_body_owners_are_visible_without_hash_substitution(self) -> None:
        rows = self.accepted()
        for name, reason in (('constant', 'const_function'), ('later', 'asynchronous'),
                             ('expanded', 'expanded'), ('VALUE', 'body_kind'), ('STATE', 'body_kind')):
            with self.subTest(name=name):
                self.assertEqual(rows[name]['reason'], reason)
        self.assertTrue(any('closure' in name and not row['eligible'] for name, row in rows.items()))
        self.assertTrue(rows['nested']['eligible'])

    def test_selected_calls_changed_source_and_ignored_spans_refuse_without_output(self) -> None:
        for changes, flags in (({'selected': ['main.rs::run']}, ()),
                               ({'sources': {str(self.entry): '0' * 64}}, ()),
                               ({}, ('-Zincremental-ignore-spans=yes',))):
            with self.subTest(changes=changes, flags=flags):
                result, output = self.inspect(changes=changes, flags=flags)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'internal compiler error', result.stderr)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
