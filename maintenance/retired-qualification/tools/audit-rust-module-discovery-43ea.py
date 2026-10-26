"""Read exact-head module records and raw coverage without executing a target."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile

BASE = Path('/Users/sclarke/github/otelc-qualification')
OUT = BASE / '2026-10-26/rust-module-root-identity/hosted'
HEAD = '43ea23604b8531c79d2ae6626bcf8d7f3bca3222'
COV = Path('/opt/homebrew/opt/llvm@22/bin/llvm-cov')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def api(path):
    return json.loads(subprocess.check_output(['python3', '-B', str(BASE / 'tools/quux-remote.py'),
        'api', 'repos/quuxio/otelc/' + path]))


def hold(manifest):
    for name, expected in manifest['source_facts'].items():
        path = Path(manifest['root']) / name
        info = path.stat()
        assert [digest(path.read_bytes()), info.st_dev, info.st_ino, info.st_mode,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns] == expected, name
        assert [info.st_uid, info.st_gid, info.st_nlink] == manifest['extra_facts'][name]
    assert digest(Path(manifest['archive']).read_bytes()) == manifest['archive_sha256']


def main():
    artifact = api('actions/artifacts/12630943661')
    job = api('actions/jobs/121425648178')
    assert artifact['workflow_run']['head_sha'] == HEAD
    assert artifact['workflow_run']['id'] == job['run_id'] == 40278275115
    assert job['status'] == 'completed' and job['conclusion'] == 'success'
    original = (OUT / 'discovery-original.zip').read_bytes()
    assert artifact['size_in_bytes'] == len(original)
    assert artifact['digest'] == 'sha256:' + digest(original)
    manifest = json.loads((OUT.parent / 'source/source.json').read_text())
    assert manifest['head'] == HEAD and len(manifest['source_facts']) == 555
    hold(manifest)
    observations = []
    with zipfile.ZipFile(OUT / 'discovery-original.zip') as saved:
        names = saved.namelist()
        assert len(names) == len(set(names)) and saved.testzip() is None
        assert saved.read('head.txt').decode().strip() == HEAD
        unit = saved.read('compiler-tests.log')
        assert b'10 passed; 0 failed; 0 ignored;' in unit
        log = (OUT / 'discovery-original.log').read_text()
        assert ['5', '8'] == re.findall(r'Ran (\d+) tests[^\n]*', log)[-2:]
        expected = {}
        for filename in ('module-rust-coverage.json', 'rust-coverage.json'):
            for block in json.loads(saved.read(filename))['data']:
                for row in block['files']:
                    assert row['summary']['lines']['percent'] >= 80
                    expected[row['filename']] = row['summary']['lines']
        assert len(expected) == 2
        tools = json.loads(saved.read('profiles/module-discovery/compiler-inputs.json'))
        binary = saved.read('compiler/debug/otelc-rust-compiler')
        assert digest(binary) == next(v for k, v in tools.items() if k.endswith('/otelc-rust-compiler'))
        with tempfile.TemporaryDirectory(prefix='coverage-read-', dir=OUT) as directory:
            root = Path(directory)
            image = root / 'compiler-object'
            profile = root / 'compiler.profdata'
            image.write_bytes(binary)
            image.chmod(0o600)
            profile.write_bytes(saved.read('lifetimes.profdata'))
            profile.chmod(0o600)
            exported = subprocess.check_output([str(COV), 'export', str(image),
                '-instr-profile=' + str(profile), *expected], stderr=subprocess.PIPE)
            (OUT / 'trusted-llvm-export.json').write_bytes(exported)
        complete = {row['filename']: row['summary']['lines']
                    for block in json.loads(exported)['data'] for row in block['files']}
        observed = {name: complete[name] for name in expected}
        assert observed == expected
        python = json.loads(saved.read('module-python-coverage.json'))['files']
        assert len(python) == 2 and all(row['summary']['percent_covered'] >= 80 for row in python.values())
        inventories = [name for name in names if '/module-discovery/' in name and name.endswith('.inventory.json')]
        assert len(inventories) == 3
        for name in inventories:
            value = json.loads(saved.read(name))
            assert value['schema_version'] == 1 and value['kind'] == 'otelc.rust.module-inventory'
            assert value['mode'] == 'module-inspect' and value['crate_name'] == 'module_map'
            assert value['byte_coordinates'] == 'original-utf8' and value['source_complete'] is False
            for source, expected_hash in value['sources'].items():
                assert digest(saved.read(source.split('/build/rust-discovery/', 1)[1])) == expected_hash
            rows = {row['compiler_name']: row for row in value['modules']}
            assert rows['']['parent'] is None and rows['']['kind'] == 'crate'
            for row in rows.values():
                for field in ('declaration', 'body'):
                    location = row[field]
                    raw = saved.read(location['file'].split('/build/rust-discovery/', 1)[1])
                    assert 0 <= location['start'] <= location['end'] <= len(raw)
            if 'unicode' in name:
                location = rows['café']['declaration']
                raw = saved.read(location['file'].split('/build/rust-discovery/', 1)[1])
                assert raw.startswith(b'\xef\xbb\xbf') and b'\r\n' in raw
                assert location['start'] == 3 and raw[location['start']:location['end']].decode() == 'mod café { fn value() {} }'
            else:
                assert rows['outer']['parent'] == '' and rows['outer::inner']['parent'] == 'outer'
                assert rows['tree']['kind'] == rows['tree::leaf']['kind'] == 'external'
                assert rows['inline']['kind'] == 'inline' and rows['inline::child']['parent'] == 'inline'
                assert rows['aliased']['body']['file'].endswith('/alternate/items.rs')
                assert rows['expanded']['reason'] == 'expanded' and not rows['expanded']['eligible']
                assert rows['main::block']['reason'] == 'block_module' and not rows['main::block']['eligible']
                assert rows['tree::leaf']['body']['start'] == rows['tree::leaf']['body']['end'] == 0
                assert 'nonexistent' not in rows
            observations.append({'inventory': name, 'modules': len(rows), 'source_inputs': len(value['sources'])})
        results = [json.loads(saved.read(name)) for name in names
                   if '/module-discovery/' in name and name.endswith('.result.json')]
        assert len(results) == 6 and sum(row['exit_code'] == 0 for row in results) == 3
        assert not any('/module-discovery/' in name and name.endswith('.application') for name in names)
    hold(manifest)
    result = {'status': 'pass for exact-head Linux SDK-free compiler module component',
        'head': HEAD, 'run': 40278275115, 'job': 121425648178, 'artifact': artifact,
        'original_zip_sha256': digest(original), 'original_log_sha256': digest((OUT / 'discovery-original.log').read_bytes()),
        'local_source_facts_held': 555, 'request_unit_tests': 10,
        'actual_module_methods': 5, 'reader_methods': 8, 'compiler_invocations': 6,
        'successful_inventories': observations, 'expected_refusals': 3,
        'raw_recomputed_rust_line_coverage': observed,
        'export_scope': 'Trusted export also includes dependency/other-module rows. Only the two explicitly required focused source files are compared here; full unfiltered export is retained. Whole-compiler floors remain a separate full gate.',
        'python_coverage': {name: row['summary'] for name, row in python.items()},
        'trusted_reader': {'path': str(COV), 'sha256': digest(COV.read_bytes()),
                           'version': subprocess.check_output([str(COV), '--version'], text=True).strip()},
        'temporary_object_profile_copies': 'removed after trusted read; originals kept in ZIP',
        'local_compiler_sdk_builds': 0, 'local_retained_program_starts': 0,
        'limits': 'Focused compiler job and application-source byte records only. CI head identifies build source; full source-bound protected analysis still required. No helper injection/private invocation, SDK target execution, macOS proof, full compiler/dependency closure or provider/shipping admission.'}
    path = OUT / 'discovery-audit.json'
    path.write_text(json.dumps(result, indent=2) + '\n')
    print(path, digest(path.read_bytes()), observed)


if __name__ == '__main__':
    main()
