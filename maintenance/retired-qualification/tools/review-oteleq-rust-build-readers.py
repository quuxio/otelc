"""Independent frozen-source review of rebuilt Rust consumer metadata readers."""
from __future__ import annotations
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

BASE = Path('/Users/sclarke/github/otelc-qualification')
ROOT = Path('/Users/sclarke/github/oteleq-qualification/work/oteleq-rust-build-readers')
OUT = BASE / '2026-10-26/oteleq-rust-readers-a503-review'
MANIFEST = Path('/Users/sclarke/github/oteleq-qualification/2026-10-26/rust-build-reader-source/source.json')
HEAD = 'a503accf31c0c8d10169658ef507ab000877b5cc'

def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()

def hold() -> int:
    value = json.loads(MANIFEST.read_bytes())
    assert value['head'] == HEAD and Path(value['root']) == ROOT
    for name, facts in value['source_facts'].items():
        path = ROOT / name
        stat = path.stat()
        assert [sha(path.read_bytes()), stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] == facts, name
        assert [stat.st_uid, stat.st_gid, stat.st_nlink] == value['extra_facts'][name], name
    assert sha(Path(value['archive']).read_bytes()) == value['archive_sha256']
    return len(value['source_facts'])

OUT.mkdir(mode=0o700)
held = hold()
assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() == HEAD
assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT)
modules = ['rust_tools', 'rust_protocol', 'rust_inventory', 'rust_lifetimes']
embedded = (ROOT / 'src/generation.rs').read_text()
identity = (ROOT / 'adapters/generation/generation.py').read_text()
for name in modules:
    assert 'include_str!("../adapters/generation/' + name + '.py")' in embedded
    assert "'" + name + ".py'" in identity
results = []
for version in ('312', '314'):
    python = Path('/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-' + version + '/bin/python')
    coverage = OUT / (version + '.coverage')
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(ROOT / 'tests')}
    command = [str(python), '-B', '-m', 'coverage', 'run', '--data-file=' + str(coverage),
        '--include=' + ','.join('*/' + name + '.py' for name in modules), '-m', 'unittest',
        'test_generation_rust_tools', 'test_generation_rust_protocol', 'test_generation_rust_inventory',
        'test_generation_rust_lifetimes', 'test_generation_native_prepared_inputs',
        'test_generation_native_prepared_build.NativeToolchainAndSDKTests', '-v']
    completed = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    (OUT / (version + '.log')).write_bytes(completed.stdout)
    assert completed.returncode == 0, completed.stdout.decode()
    count = int(re.search(rb'Ran (\d+) tests?', completed.stdout).group(1))
    assert count == 44
    coverage_json = OUT / (version + '-coverage.json')
    subprocess.run([str(python), '-B', '-m', 'coverage', 'json', '--data-file=' + str(coverage), '-o', str(coverage_json)], cwd=ROOT, env=env, check=True, capture_output=True)
    rows = json.loads(coverage_json.read_bytes())['files']
    assert len(rows) == 4
    floors = {name: row['summary'] for name, row in rows.items()}
    assert all(row['percent_covered'] == 100 for row in floors.values())
    results.append({'python': version, 'test_methods': count, 'exit': completed.returncode, 'module_coverage': floors})
for command in ([ '/opt/homebrew/bin/markdownlint', 'README.md', 'docs/completion.md', 'docs/rust-prepared-provider.md'], ['git', 'diff', '--check']):
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
assert hold() == held
value = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'head': HEAD,
    'status': 'clear for the exact source/declaration and lifetime metadata reader component',
    'source_facts_held_before_and_after': held, 'archive_sha256': json.loads(MANIFEST.read_bytes())['archive_sha256'],
    'results': results, 'findings': [], 'review': [
        'Closed compiler inventory1 and woven2 declarations bind caller-frozen canonical original files, byte bounds, digests, ordered identities and complete source denominator. No producer-selected filesystem reads or SDK renderer imports.',
        'All body/local/type/site/skipped data is checked before selection. Unavailable selected foreign/container types refuse; argument/return/unavailable layout shapes stay explicit compiler requests, never silently dropped.',
        'Complete unfiltered MIR snapshots use compact sorted UTF-8 hashing matching SDK rust_lifetimes.weave_request and compiler snapshots. Selection excludes apply to body and original type sources. Woven maps/fingerprints require caller-held requests.',
        'Both new assets are Rust-embedded and generator-identity-bound, covered by altered-identity controls. The per-module 80% floors are in both focused and full generation targets.',
        'Original runtime provider selection is unchanged. Documentation preserves exact earlier qualified scopes and clearly leaves compiler input discovery, full build/prepared/launch readers and real generated execution open.'
    ], 'local_sdk_builds': 0, 'local_compiler_builds': 0, 'local_application_starts': 0,
    'limits': 'Literal metadata/refusal components and source review only. No actual Rust distribution build, compiler discovery/execution, dependency/sysroot closure, complete SDK build/launch binding, OTLP, generated/retained or shipping qualification. Hosted protected CI remains separately required.',
    'controller_sha256': sha(Path(__file__).read_bytes())}
(OUT / 'review.json').write_text(json.dumps(value, indent=2) + '\n')
print(json.dumps({'status': value['status'], 'head': HEAD, 'facts_held': held, 'methods_each_python': 44, 'receipt_sha256': sha((OUT/'review.json').read_bytes())}))
