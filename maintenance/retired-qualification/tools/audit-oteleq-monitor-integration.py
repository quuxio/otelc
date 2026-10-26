"""Check shared-monitor extraction and the exact reviewed main integration."""
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess

BASE = Path('/Users/sclarke/github/otelc-qualification')
PEER = Path('/Users/sclarke/github/oteleq-qualification')
OUT = BASE / '2026-10-26/oteleq-rust-monitor-7e69012-review'


def hold(name):
    manifest = json.loads((PEER / '2026-10-26' / name / 'source.json').read_text())
    root = Path(manifest['root'])
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip() == manifest['head']
    for filename, row in manifest['source_facts'].items():
        path = root / filename
        info = path.stat()
        assert [hashlib.sha256(path.read_bytes()).hexdigest(), info.st_dev, info.st_ino,
                info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns] == row, filename
        assert [info.st_uid, info.st_gid, info.st_nlink] == manifest['extra_facts'][filename]
    assert hashlib.sha256(Path(manifest['archive']).read_bytes()).hexdigest() == manifest['archive_sha256']
    return manifest, root


def dump(node):
    return ast.dump(node, include_attributes=False)


class Substitute(ast.NodeTransformer):
    def __init__(self, replacements):
        self.replacements = replacements

    def visit_Name(self, node):
        return copy.deepcopy(self.replacements.get(node.id, node))


def main():
    monitor, root = hold('rust-owned-monitor-source-02')
    integration, integrated = hold('rust-prepared-main-source')
    assert monitor['head'] == '7e6901289409f8b9cc2f19308567a7a1a5734555'
    assert integration['head'] == 'f8d06b78e65b270e0aceb4cfffac525ddc1b0913'
    parent = 'b9f7c6c7fad6db6d48c24af1f7a75d1557615de7'
    before = ast.parse(subprocess.check_output(['git', 'show', parent + ':adapters/generation/native_session.py'], cwd=root))
    after = ast.parse((root / 'adapters/generation/native_session.py').read_text())
    old = {node.name: node for node in before.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    new = {node.name: node for node in after.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    for name in ('kernel', 'publish'):
        assert dump(old[name]) == dump(new[name]), name
    restored = copy.deepcopy(new['Session'])
    methods = {node.name: node for node in restored.body if isinstance(node, ast.FunctionDef)}
    poll = methods['poll']
    positions = [i for i, node in enumerate(poll.body)
                 if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                 and isinstance(node.value.func, ast.Name) and node.value.func.id == 'observed_owner']
    assert len(positions) == 1
    expanded = copy.deepcopy(new['observed_owner'].body[1:-1])
    expanded = [Substitute({'reason': ast.Constant('native application is not the live owned waiting child')}).visit(node)
                for node in expanded]
    poll.body[positions[0]:positions[0] + 1] = expanded
    methods['close'].body[1:] = [Substitute({'child': ast.parse('self.child', mode='eval').body}).visit(copy.deepcopy(node))
                               for node in new['close_child'].body[1:]]
    assert dump(old['Session']) == dump(restored), 'native Session changed beyond exact helper extraction'
    retained = copy.deepcopy(new['retained'])
    retained.body[2:] = [Substitute({'close': ast.parse('session.close', mode='eval').body}).visit(copy.deepcopy(node))
                         for node in new['cleanup'].body[1:]]
    assert dump(old['retained']) == dump(retained), 'native error/cleanup semantics changed'
    embed = (root / 'src/generation.rs').read_text()
    identity = ast.parse((root / 'adapters/generation/generation.py').read_text())
    assert 'include_str!("../adapters/generation/rust_session.py")' in embed
    assert any(isinstance(node, ast.Constant) and node.value == 'rust_session.py' for node in ast.walk(identity))
    parents = subprocess.check_output(['git', 'show', '-s', '--format=%P', integration['head']], cwd=integrated, text=True).split()
    assert parents == [monitor['head'], '29337ec9b9c07ccf3677aed2f536486d0b9bb359']
    assert subprocess.check_output(['git', 'rev-parse', integration['head'] + '^{tree}'], cwd=root) == subprocess.check_output(['git', 'rev-parse', monitor['head'] + '^{tree}'], cwd=root)
    original = json.loads((OUT / 'review.json').read_text())
    assert [item['methods'] for item in original['tests']] == [101, 101]
    for version in ('312', '314'):
        log = (OUT / (version + '.log')).read_text()
        assert 'Ran 101 tests' in log and '\nOK\n' in log
        assert 'test_real_python_gate_processes_verify_kernel_parent_groups_and_release_order' in log
    hold('rust-owned-monitor-source-02')
    hold('rust-prepared-main-source')
    result = {'status': 'clear within reviewed source, controlled Python OS-monitor and lineage scope',
              'monitor_head': monitor['head'], 'integration_head': integration['head'],
              'source_facts_each': 690, 'integration_parents': parents,
              'native_preservation': 'AST-identical after inlining observed_owner, close_child and cleanup; kernel/publish unchanged',
              'tests': original['tests'], 'new_monitor_embedding_and_generator_identity': True,
              'integration_tree_exact_monitor': True, 'findings': [],
              'clarification_of_original_receipt': 'The tests DO execute controlled Python supervisor/child helpers and query actual OS ownership. They do not execute SDK/Rust target applications. Original receipt limit wording was too broad.',
              'limits': 'No Rust/native/compiler/SDK builds or SDK target runs; not provider admission, executed-image proof, original transport qualification or full source/dependency closure.'}
    target = OUT / 'integration-review.json'
    target.write_text(json.dumps(result, indent=2) + '\n')
    print(target, hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
