"""Independent source/literal checks of prepared Rust readers and main lineage."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BASE = Path('/Users/sclarke/github/otelc-qualification')
PEER = Path('/Users/sclarke/github/oteleq-qualification')


def hold(manifest):
    root = Path(manifest['root'])
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip() == manifest['head']
    for name, row in manifest['source_facts'].items():
        path = root / name
        stat = path.stat()
        assert [hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_dev, stat.st_ino, stat.st_mode,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] == row, name
        assert [stat.st_uid, stat.st_gid, stat.st_nlink] == manifest['extra_facts'][name], name
    assert hashlib.sha256(Path(manifest['archive']).read_bytes()).hexdigest() == manifest['archive_sha256']


def check(role, manifest_name, head, count):
    manifest = json.loads((PEER / '2026-10-26' / manifest_name / 'source.json').read_text())
    assert manifest['head'] == head
    hold(manifest)
    root = Path(manifest['root'])
    out = BASE / ('2026-10-26/oteleq-rust-' + role + '-' + head[:7] + '-review')
    out.mkdir(mode=0o700, exist_ok=True)
    tests = ['test_generation_rust_tools', 'test_generation_rust_protocol', 'test_generation_rust_inventory',
             'test_generation_rust_lifetimes', 'test_generation_rust_source_build', 'test_generation_rust_owned_build']
    modules = ['rust_tools', 'rust_protocol', 'rust_inventory', 'rust_lifetimes', 'rust_build_recipe',
               'rust_source_build', 'rust_owned_build']
    if role in ('prepared', 'monitor', 'receipt'):
        tests += ['test_generation_rust_prepared', 'test_generation_rust_execution',
                  'test_generation_native_execution', 'test_generation_native_prepared_inputs',
                  'test_generation_native_prepared_build.NativeToolchainAndSDKTests']
        modules += ['rust_prepared', 'rust_health', 'rust_execution', 'native_health']
    else:
        tests += ['test_generation_native_prepared_inputs',
                  'test_generation_native_prepared_build.NativeToolchainAndSDKTests']
    if role in ('monitor', 'receipt'):
        tests += ['test_generation_rust_session', 'test_generation_native_session']
        modules += ['rust_session', 'native_session']
    if role == 'receipt':
        tests += ['test_generation_rust_telemetry']
        modules += ['rust_telemetry']
    results = []
    for version in ('312', '314'):
        python = str(PEER / ('2026-10-24/python-live-metrics/env-' + version + '/bin/python'))
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root / 'tests')}
        data = out / (version + '.coverage')
        include = ','.join('*/' + name + '.py' for name in modules)
        command = [python, '-B', '-m', 'coverage', 'run', '--data-file=' + str(data), '--include=' + include,
                   '-m', 'unittest', *tests, '-v']
        saved = out / (version + '.log')
        if '--reuse-completed-tests' in sys.argv and saved.exists():
            raw = saved.read_bytes()
            assert b'\nOK\n' in raw and b'\nFAILED' not in raw and b'\nERROR:' not in raw
        else:
            completed = subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=180)
            raw = completed.stdout + completed.stderr
            saved.write_bytes(raw)
            assert completed.returncode == 0, raw.decode()
        assert int(re.search(rb'Ran (\d+) tests', raw).group(1)) == count
        report = out / (version + '-coverage.json')
        subprocess.run([python, '-B', '-m', 'coverage', 'json', '--data-file=' + str(data), '-o', str(report)],
                       cwd=root, env=env, capture_output=True, check=True)
        files = json.loads(report.read_text())['files']
        assert len(files) == len(modules) and all(row['summary']['percent_covered'] >= 80 for row in files.values())
        results.append({'python': version, 'methods': count,
                        'coverage': {name: row['summary'] for name, row in files.items()}})
    manual = []
    if role in ('prepared', 'monitor', 'receipt'):
        original = subprocess.check_output(['git', 'show', '80074b44a6258ad8066dfad985a11e55900def25:adapters/generation/native_health.py'], cwd=root)
        before = ast.parse(original)
        after = ast.parse((root / 'adapters/generation/native_health.py').read_text())
        old = {node.name: node for node in before.body if isinstance(node, ast.FunctionDef)}
        new = {node.name: node for node in after.body if isinstance(node, ast.FunctionDef)}
        for name, node in old.items():
            replacement = new[name]
            if name == 'validate':
                calls = [i for i, value in enumerate(replacement.body)
                         if isinstance(value, ast.Expr) and isinstance(value.value, ast.Call)
                         and isinstance(value.value.func, ast.Name) and value.value.func.id == 'engines']
                assert len(calls) == 1
                index = calls[0]
                replacement.body[index:index + 1] = new['engines'].body[1:]
            assert ast.dump(node, include_attributes=False) == ast.dump(replacement, include_attributes=False), name
        manual += ['Native health validate AST exactly restored by inlining engines; every other old function unchanged.']
        embed = (root / 'src/generation.rs').read_text()
        identity = (root / 'adapters/generation/generation.py').read_text()
        for name in ('rust_prepared', 'rust_health', 'rust_execution', 'rust_session', 'rust_telemetry'):
            assert 'include_str!("../adapters/generation/' + name + '.py")' in embed
            assert any(isinstance(node, ast.Constant) and node.value == name + '.py'
                       for node in ast.walk(ast.parse(identity)))
            tree = ast.parse((root / ('adapters/generation/' + name + '.py')).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or '').startswith('otelc.')
                if isinstance(node, ast.Import):
                    assert all(not value.name.startswith('otelc.') for value in node.names)
        manual += ['Prepared, monitor and receipt readers included in Rust distribution and generator identity; no SDK renderer imports.',
                   'Manually compared intended image, request/plan canonical hashes, literal argv, source selection and health1/5 with frozen SDK395 sources.',
                   'Caller Owner remains an external OS-monitor expectation, not an observation produced by literal readers; completed/censored drop counters stay separate.']
    else:
        parents = subprocess.check_output(['git', 'show', '-s', '--format=%P', head], cwd=root, text=True).split()
        assert parents == ['da94baf4ae460bc4a4b197924e0fa0b97ca57602', '0455957a622530a5e94f28f797bff9272537b11a']
        tree = subprocess.check_output(['git', 'rev-parse', head + '^{tree}'], cwd=root)
        assert tree == subprocess.check_output(['git', 'rev-parse', 'da94baf4ae460bc4a4b197924e0fa0b97ca57602^{tree}'], cwd=root)
        manual += ['Normal merge lineage from previously reviewed da94; entire tracked tree identical to da94.']
    subprocess.run(['git', 'diff', '--check', manifest['base'], head], cwd=root, check=True, capture_output=True)
    hold(manifest)
    receipt = {'status': 'clear within reviewed component source/literal scope', 'head': head,
               'source_facts': len(manifest['source_facts']), 'archive_sha256': manifest['archive_sha256'],
               'tests': results, 'findings': [], 'manual_review': manual,
               'local_native_compiler_sdk_builds': 0, 'local_target_applications': 0,
               'limits': 'Controlled Python helper OS ownership is exercised. No Rust/SDK target execution, native/compiler builds, original transport receipt, generated/retained provider admission, full compiler/sysroot closure or shipping qualification.'}
    path = out / 'review.json'
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    print(role, head, count, hashlib.sha256(path.read_bytes()).hexdigest())



check('receipt', 'rust-function-receipt-source', '26fe8d1cf97e6fad12be7dee81aae5dc3b228738', 108)
