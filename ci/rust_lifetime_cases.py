"""Actual SDK-free Rust compiler lifetime discovery; no application is executed."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from otelc import rust_lifetimes
from otelc.config import Policy

MODEL = '''pub struct Item {pub value:i32}
impl Drop for Item {fn drop(&mut self){std::hint::black_box(self.value);}}
pub type Alias = Item;
pub struct Zero;
impl Drop for Zero {fn drop(&mut self){}}
#[derive(Clone,Copy)] pub struct CopyItem {pub value:i32}
pub struct Generic<T> {pub value:T}
impl<T> Drop for Generic<T> {fn drop(&mut self){std::hint::black_box(&self.value);}}
pub enum Choice {Left(i32),Right(i32)}
pub union Raw {pub value:i32,pub other:u32}
'''
APP = '''mod model;
fn transfer(item:model::Item)->model::Item {item}
fn generic<T>(value:T) {let value=model::Generic{value};std::hint::black_box(&value);}
const fn constant()->usize {std::mem::size_of::<model::Item>()}
async fn asynchronous()->i32 {7}
macro_rules! make_fn {()=>{fn expanded()->i32{9}}}
make_fn!();
fn workload(fail:bool) {
 let item:model::Alias=model::Item{value:7};let moved=item;
 let mut returned=transfer(moved);returned.value=9;
 let copy=model::CopyItem{value:3};let copied=copy;
 let zero=model::Zero;let generic=model::Generic{value:17_u32};
 let choice=model::Choice::Left(1);let other=model::Choice::Right(2);
 let raw=model::Raw{value:3};
 std::hint::black_box((&returned,copy.value,copied.value,&zero,&generic,&choice,&other,&raw));
 if fail {panic!("original panic")}
}
fn main(){workload(false);generic(7_u64);std::hint::black_box((constant(),expanded(),asynchronous()));}
'''


def digest(path: Path) -> str:
    """Bind original Rust sources and actual compiler products."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RustLifetimeCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT']).resolve() / 'lifetime-discovery'
        cls.base.mkdir(parents=True, mode=0o700, exist_ok=False)
        cls.compiler = Path(os.environ['OTELC_RUST_COMPILER']).resolve(strict=True)
        cls.rustc = Path(os.environ['OTELC_RUSTC']).resolve(strict=True)
        version = subprocess.check_output([str(cls.rustc), '-vV'], text=True)
        if 'release: 1.98.1\n' not in version or 'LLVM version: 22.1.8\n' not in version:
            raise ValueError('lifetime discovery requires Rust1.98.1/LLVM22.1.8')
        cls.sysroot = subprocess.check_output([str(cls.rustc), '--print', 'sysroot'], text=True).strip()
        cls.held = {str(path): digest(path) for path in (cls.compiler, cls.rustc)}
        (cls.base / 'compiler-inputs.json').write_text(json.dumps(cls.held, indent=2) + '\n')
        (cls.base / 'rustc-version.txt').write_text(version)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.held != {name: digest(Path(name)) for name in cls.held}:
            raise AssertionError('Rust lifetime compiler/tool inputs changed')

    def setUp(self) -> None:
        self.folder = self.base / self._testMethodName
        self.folder.mkdir(mode=0o700)
        self.root = self.folder / 'application'; self.root.mkdir()
        self.entry = self.root / 'app.rs'; self.entry.write_text(APP)
        (self.root / 'model.rs').write_text(MODEL)
        self.sources = {str(path): digest(path) for path in self.root.glob('*.rs')}
        self.counter = 0

    def tearDown(self) -> None:
        self.assertEqual(self.sources, {name: digest(Path(name)) for name in self.sources})
        (self.folder / 'original-inputs.json').write_text(json.dumps(self.sources, indent=2) + '\n')

    def inspect(self, mode: str = 'lifetime-inspect', *, optimisation: int = 2,
                changes: dict | None = None) -> tuple[subprocess.CompletedProcess, Path, Path]:
        self.counter += 1
        label = f'command-{self.counter}'
        output = self.folder / (label + '.inventory.json')
        product = self.folder / (label + '-application')
        request = {'schema_version': 1, 'mode': mode, 'root': str(self.root), 'entry': str(self.entry),
                   'output': str(output), 'sources': self.sources, 'selected': []} | (changes or {})
        filename = self.folder / (label + '.request.json')
        filename.write_text(json.dumps(request) + '\n')
        argv = [str(self.compiler), str(self.entry), '--crate-name', 'discovery', '--edition=2024',
                '--sysroot', self.sysroot, f'-Copt-level={optimisation}', '-o', str(product)]
        environment = dict(os.environ, OTELC_RUST_REQUEST=str(filename),
            LLVM_PROFILE_FILE=str(self.base.parent / '%m-%p.profraw'))
        process = subprocess.run(argv, cwd=self.folder, env=environment,
                                 capture_output=True, timeout=120)
        (self.folder / (label + '.stdout')).write_bytes(process.stdout)
        (self.folder / (label + '.stderr')).write_bytes(process.stderr)
        (self.folder / (label + '.result.json')).write_text(json.dumps({
            'arguments': argv, 'exit_code': process.returncode}, indent=2) + '\n')
        return process, output, product

    def policy(self, **changes) -> Policy:
        return Policy.parse({'schema_version': 3, 'languages': ['rust'],
            'sources': {'include': ['**']}, 'functions': {'include': ['not-selected']},
            'lifetimes': {'enabled': True, 'include': ['*Item', '*Generic*', '*Zero', '*Choice', '*Raw']},
            **changes})

    def test_actual_resolved_aliases_layouts_moves_drops_and_explicit_skips(self) -> None:
        result, output, product = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b''); self.assertFalse(product.exists())
        selected = rust_lifetimes.read(output, self.policy(), self.root, self.sources)
        self.assertFalse(selected['source_complete']); self.assertFalse(selected['instrumentation_complete'])
        rows = [row for body in selected['bodies'] for row in body['locals']]
        items = [row for row in rows if row['compiler_type'].endswith('::Item')]
        self.assertTrue(items)
        self.assertTrue(all(row['size'] == row['alignment'] == 4 and row['needs_drop'] for row in items))
        self.assertFalse(any(row['rendered_type'].endswith('Alias') for row in rows))
        self.assertTrue(any(row['size'] == 0 and row['needs_drop'] for row in rows))
        self.assertTrue(any(row['kind'] == 'enum' for row in rows))
        self.assertTrue(any(row['kind'] == 'union' for row in rows))
        self.assertTrue(any(row['layout_error'] and row['size'] is None for row in rows))
        sites = [site for body in selected['bodies'] for site in body['sites']]
        self.assertTrue({'construction', 'move', 'copy', 'drop', 'call_argument', 'call_return'}
                        <= {site['kind'] for site in sites})
        self.assertTrue(any(site['cleanup'] for site in sites if site['kind'] == 'drop'))
        self.assertTrue(any(site['projected'] for site in sites))
        self.assertTrue({'const', 'async', 'expanded'} <= {row['reason'] for row in selected['skipped']})
        self.assertFalse(any(body['function']['asynchronous'] or body['function']['expanded']
                             for body in selected['bodies']))

    def test_common_selectors_and_off_state_filter_without_changing_originals(self) -> None:
        result, output, _ = self.inspect(); self.assertEqual(result.returncode, 0, result.stderr.decode())
        original = rust_lifetimes.read(output, self.policy(), self.root, self.sources)
        for changes in ({'lifetimes': {'enabled': False}},
                        {'lifetimes': {'enabled': True, 'include': ['*Item'], 'exclude': ['*Item']}},
                        {'sources': {'include': ['**'], 'exclude': ['model.rs']}},
                        {'sources': {'include': ['**'], 'exclude': ['app.rs']}}):
            with self.subTest(changes=changes):
                selected = rust_lifetimes.read(output, self.policy(**changes), self.root, self.sources)
                self.assertEqual(selected['bodies'], [])
        self.assertEqual(rust_lifetimes.read(output, self.policy(), self.root, self.sources), original)

    def test_normal_function_discovery_and_both_optimisation_requests_stay_supported(self) -> None:
        result, output, product = self.inspect('inspect')
        self.assertEqual(result.returncode, 0, result.stderr.decode()); self.assertFalse(product.exists())
        ordinary = json.loads(output.read_bytes())
        self.assertEqual((ordinary['schema_version'], ordinary['kind']), (1, 'otelc.rust.compiler-inventory'))
        self.assertNotIn('bodies', ordinary)
        for optimisation in (0, 3):
            result, output, product = self.inspect(optimisation=optimisation)
            self.assertEqual(result.returncode, 0, result.stderr.decode()); self.assertFalse(product.exists())
            self.assertTrue(rust_lifetimes.read(output, self.policy(), self.root, self.sources)['bodies'])

    def test_lifetime_discovery_refuses_function_selection_and_unbound_modules(self) -> None:
        for changes in ({'selected': ['app.rs::main']},
                        {'sources': {str(self.entry): self.sources[str(self.entry)]}}):
            result, output, product = self.inspect(changes=changes)
            with self.subTest(changes=changes):
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists()); self.assertFalse(product.exists())

    def test_foreign_and_owned_container_types_are_visible_and_cannot_be_woven(self) -> None:
        self.entry.write_text('''struct Item;impl Drop for Item{fn drop(&mut self){}}
struct Plain;
fn opaque<T>(value:T){std::hint::black_box(value);}
fn workload(){
 let file=std::fs::File::open("must-not-run-during-discovery").unwrap();
 let pair=(Item,Item);let array=[Item,Item];let plain=[Plain,Plain];
 let vector=Vec::<Item>::new();let borrowed=(&array,&pair);
 std::hint::black_box((&file,&pair,&array,&plain,&vector,borrowed));
}
fn main(){workload();opaque(7_u32);}
''')
        self.sources[str(self.entry)] = digest(self.entry)
        result, output, product = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(product.exists()); self.assertEqual(result.stdout, b'')
        document = json.loads(output.read_bytes())
        self.assertEqual(document['schema_version'], 2)
        rows = [row for body in document['bodies'] for row in body['unavailable_locals']]
        self.assertTrue({'foreign_adt', 'array', 'tuple', 'opaque'} <= {row['kind'] for row in rows})
        self.assertTrue(any(row['compiler_type'].endswith('::File') for row in rows))
        self.assertTrue(any(row['kind'] == 'array' and not row['needs_drop'] for row in rows))
        self.assertFalse(any(row['kind'] == 'tuple' and row['rendered_type'].startswith('(&') for row in rows))
        for include in (['*File*'], ['[Item;*'], ['(Item,*'], ['T']):
            policy = self.policy(lifetimes={'enabled': True, 'include': include})
            with self.subTest(include=include), self.assertRaisesRegex(ValueError, 'unavailable'):
                rust_lifetimes.weave_request(output, policy, self.root, self.sources)


def main() -> int:
    """Keep genuine selected/executed counts; a skipped class cannot qualify."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RustLifetimeCases)
    selected = suite.countTestCases(); result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT'])
    (output / 'lifetime-discovery-result.json').write_text(json.dumps({
        'selected': selected, 'tests_run': result.testsRun, 'skipped': len(result.skipped),
        'failures': len(result.failures), 'errors': len(result.errors),
        'successful': result.wasSuccessful(), 'application_launches': 0}, indent=2) + '\n')
    return 0 if selected == 5 and result.testsRun == selected and not result.skipped and result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
