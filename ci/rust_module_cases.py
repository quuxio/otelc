"""Actual compiler module-map checks; no SDK or target application is started."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

from otelc import rust_modules

FILES = {
    'main.rs': '''mod outer;
mod tree;
mod inline { #[path="child.rs"] mod child; }
#[path="alternate/items.rs"] mod aliased;
#[cfg(feature="selected")] mod nonexistent;
macro_rules! make_module { () => { mod expanded { pub fn value() -> i32 { 9 } } } }
make_module!();
fn main() { mod block { pub fn value() -> i32 { 7 } } std::hint::black_box(block::value()); }
''',
    'outer.rs': 'mod inner; struct Private(i32); impl Private {fn new() -> Self {Self(1)}}\n',
    'outer/inner.rs': 'fn private_work() -> i32 { 11 }\n',
    'tree/mod.rs': 'mod leaf;\n',
    'tree/leaf.rs': '',
    'inline/child.rs': 'fn private_work() {}\n',
    'alternate/items.rs': 'struct Receiver; impl Receiver {fn work(&self) {}}\n',
}


def digest(path: Path) -> str:
    """Identify physical original sources and compiler inputs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RustModuleCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(os.environ['OTELC_RUST_COMPONENT_OUTPUT']).resolve() / 'module-discovery'
        cls.base.mkdir(mode=0o700, parents=True, exist_ok=False)
        cls.compiler = Path(os.environ['OTELC_RUST_COMPILER']).resolve(strict=True)
        cls.rustc = Path(os.environ['OTELC_RUSTC']).resolve(strict=True)
        version = subprocess.check_output([str(cls.rustc), '-vV'], text=True)
        if 'release: 1.98.1\n' not in version or 'LLVM version: 22.1.8\n' not in version:
            raise ValueError('module discovery requires Rust1.98.1/LLVM22.1.8')
        cls.sysroot = subprocess.check_output([str(cls.rustc), '--print', 'sysroot'], text=True).strip()
        cls.tools = {str(path): digest(path) for path in (cls.compiler, cls.rustc)}
        (cls.base / 'compiler-inputs.json').write_text(json.dumps(cls.tools, indent=2) + '\n')
        (cls.base / 'rustc-version.txt').write_text(version)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.tools != {name: digest(Path(name)) for name in cls.tools}:
            raise AssertionError('module discovery compiler inputs changed')

    def setUp(self) -> None:
        self.folder = self.base / self._testMethodName
        self.folder.mkdir(mode=0o700)
        self.root = self.folder / 'application'
        self.root.mkdir()
        for name, text in FILES.items():
            if name == 'main.rs' and self._testMethodName == 'test_unicode_crlf_and_bom_use_physical_utf8_byte_coordinates':
                text = '\ufeffmod café { fn value() {} }\r\nfn main() {}\r\n'
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.entry = self.root / 'main.rs'
        self.sources = {str(path): digest(path) for path in sorted(self.root.rglob('*.rs'))}
        self.counter = 0

    def tearDown(self) -> None:
        self.assertEqual(self.sources, {name: digest(Path(name)) for name in self.sources})
        (self.folder / 'original-inputs.json').write_text(json.dumps(self.sources, indent=2) + '\n')

    def inspect(self, *, changes: dict | None = None, flags: tuple[str, ...] = ()):
        self.counter += 1
        label = 'command-' + str(self.counter)
        output = self.folder / (label + '.inventory.json')
        image = self.folder / (label + '.application')
        request = {'schema_version': 1, 'mode': 'module-inspect', 'root': str(self.root),
                   'entry': str(self.entry), 'sources': self.sources, 'selected': [],
                   'output': str(output)} | (changes or {})
        filename = self.folder / (label + '.request.json')
        filename.write_text(json.dumps(request) + '\n')
        argv = [str(self.compiler), str(self.entry), '--crate-name', 'module_map', '--edition=2024',
                '--sysroot', self.sysroot, '-Copt-level=2', *flags, '-o', str(image)]
        env = {**os.environ, 'OTELC_RUST_REQUEST': str(filename),
               'LLVM_PROFILE_FILE': str(self.base.parent / '%m-%p.profraw')}
        result = subprocess.run(argv, cwd=self.folder, env=env, capture_output=True, timeout=120)
        (self.folder / (label + '.stdout')).write_bytes(result.stdout)
        (self.folder / (label + '.stderr')).write_bytes(result.stderr)
        (self.folder / (label + '.result.json')).write_text(json.dumps({
            'arguments': argv, 'exit_code': result.returncode}, indent=2) + '\n')
        self.assertFalse(image.exists(), 'module discovery must not emit a target image')
        return result, output

    def accepted(self, **arguments) -> dict:
        result, output = self.inspect(**arguments)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return rust_modules.read(output, self.root, self.entry, self.sources)

    def test_nested_inline_external_path_and_empty_modules_keep_original_locations(self) -> None:
        document = self.accepted()
        rows = {row['compiler_name']: row for row in document['modules']}
        for name, parent, filename, kind in (
            ('outer', '', 'outer.rs', 'external'),
            ('outer::inner', 'outer', 'outer/inner.rs', 'external'),
            ('tree', '', 'tree/mod.rs', 'external'),
            ('tree::leaf', 'tree', 'tree/leaf.rs', 'external'),
            ('inline', '', 'main.rs', 'inline'),
            ('inline::child', 'inline', 'inline/child.rs', 'external'),
            ('aliased', '', 'alternate/items.rs', 'external'),
        ):
            with self.subTest(name=name):
                row = rows['module_map::' + name]
                self.assertEqual(row['parent'], 'module_map' + ('::' + parent if parent else ''))
                self.assertEqual(row['body']['file'], str(self.root / filename))
                self.assertEqual(row['kind'], kind)
                self.assertTrue(row['eligible'])
                self.assertIsNone(row['reason'])
        self.assertEqual(rows['module_map']['body']['file'], str(self.entry))
        leaf = rows['module_map::tree::leaf']['body']
        self.assertEqual((leaf['start'], leaf['end']), (0, 0))

    def test_macro_and_block_routes_are_visible_but_cannot_gain_private_caller_access(self) -> None:
        rows = {row['compiler_name']: row for row in self.accepted()['modules']}
        for name, reason in (('module_map::expanded', 'expanded'),
                             ('module_map::main::block', 'block_module')):
            with self.subTest(name=name):
                self.assertFalse(rows[name]['eligible'])
                self.assertEqual(rows[name]['reason'], reason)
        self.assertFalse(any(name.endswith('::nonexistent') for name in rows))

    def test_cfg_selected_missing_module_is_a_real_compiler_failure_not_an_invented_route(self) -> None:
        result, output = self.inspect(flags=('--cfg', 'feature="selected"'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'nonexistent', result.stderr)
        self.assertFalse(output.exists())

    def test_selection_and_changed_source_requests_refuse_before_inventory_publication(self) -> None:
        result, output = self.inspect(changes={'selected': ['main.rs::main']})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        wrong = self.sources | {str(self.entry): '0' * 64}
        result, output = self.inspect(changes={'sources': wrong})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_unicode_crlf_and_bom_use_physical_utf8_byte_coordinates(self) -> None:
        document = self.accepted()
        rows = {row['compiler_name']: row for row in document['modules']}
        declaration = rows['module_map::café']['declaration']
        original = self.entry.read_bytes()
        self.assertTrue(original.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(original[declaration['start']:declaration['end']].decode(),
                         'mod café { fn value() {} }')
        self.assertEqual(declaration['start'], 3)
        body = rows['module_map']['body']
        self.assertIn(b'\r\n', original[body['start']:body['end']])
        self.assertEqual(document['byte_coordinates'], 'original-utf8')


if __name__ == '__main__':
    unittest.main(verbosity=2)
