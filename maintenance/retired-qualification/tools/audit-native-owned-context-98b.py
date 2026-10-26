"""Audit native context SDK bytes and coverage without executing a retained app."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import zipfile


BASE = Path('/Users/sclarke/github/otelc-qualification')
OUTPUT = BASE / '2026-10-26/native-owned-task-context/hosted'
HEAD = '98b3cfedd685170cfa71e71b2a7e6f10ad40c416'
RUN = 40255675498
JOB = 121356884068
ARTIFACT = 12622920155
COV = Path('/opt/homebrew/opt/llvm@22/bin/llvm-cov')


def digest(raw: bytes) -> str:
    """Identify retained input bytes."""
    return hashlib.sha256(raw).hexdigest()


def api(path: str) -> bytes:
    """Read only through the identity-checked quuxio helper."""
    return subprocess.check_output([
        '/usr/bin/python3', str(BASE / 'tools/quux-remote.py'), 'api',
        'repos/quuxio/otelc/' + path,
        *(['--allow-escape-sequences'] if path.endswith('/logs') else []),
    ])


def hold(manifest: dict) -> int:
    """Require original source facts, extra facts and archive to hold."""
    for name, expected in manifest['source_facts'].items():
        path = Path(manifest['root']) / name
        stat = path.stat()
        assert [digest(path.read_bytes()), stat.st_dev, stat.st_ino, stat.st_mode,
                stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns] == expected, name
        assert [stat.st_uid, stat.st_gid, stat.st_nlink] == manifest['extra_facts'][name]
    assert digest(Path(manifest['archive']).read_bytes()) == manifest['archive_sha256']
    return len(manifest['source_facts'])


def audit() -> dict:
    """Bind original CTest assertions, source and profiles to their actual job."""
    meta = json.loads(api(f'actions/artifacts/{ARTIFACT}'))
    job = json.loads(api(f'actions/jobs/{JOB}'))
    assert meta['workflow_run']['head_sha'] == HEAD
    assert meta['workflow_run']['id'] == job['run_id'] == RUN
    assert job['status'] == 'completed' and job['conclusion'] == 'success'
    archive_bytes = (OUTPUT / 'linux-original.zip').read_bytes()
    assert len(archive_bytes) == meta['size_in_bytes']
    assert meta['digest'] == 'sha256:' + digest(archive_bytes)
    manifest = json.loads((OUTPUT.parent / 'source/source.json').read_text())
    assert manifest['head'] == HEAD
    held = hold(manifest)
    log = api(f'actions/jobs/{JOB}/logs')
    with (OUTPUT / 'linux-original.log.gz').open('xb') as stream:
        stream.write(gzip.compress(log, mtime=0))
    analysis = OUTPUT / 'read-only-analysis'
    analysis.mkdir(mode=0o700)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        assert archive.read('native-context/head.txt').decode().strip() == HEAD
        assert not archive.read('native-context/source-status.txt')
        source_tar = archive.read('native-context/source.tar')
        assert digest(source_tar) == manifest['archive_sha256']
        with tarfile.open(fileobj=io.BytesIO(source_tar)) as source:
            members = [row for row in source.getmembers() if row.isfile()]
            assert len(members) == held
            assert {row.name for row in members} == set(manifest['source_facts'])
            for row in members:
                assert digest(source.extractfile(row).read()) == manifest['source_facts'][row.name][0]
        ctest = archive.read('native-sdk-tests.log').decode()
        assert ctest.count('Test Passed.') == 3
        assert ctest.count('native owned context: 5 SDK groups passed') == 1
        assert 'native SDK function engine: compiler-linked C/C++ group passed' in ctest
        record = json.loads(archive.read('native-control-coverage.json'))
        source_inputs = record['source_inputs']
        for item in source_inputs:
            name = item['path'].split('/otelc/otelc/', 1)[1]
            assert item['sha256'] == manifest['source_facts'][name][0]
        database = json.loads(archive.read('native-sdk-compile-commands.json'))
        test_rows = [row for row in database if row['file'].endswith('/adapters/native/tests/function_runtime_test.cpp')]
        assert len(test_rows) == 1
        flags = test_rows[0].get('arguments') or shlex.split(test_rows[0]['command'])
        assert [flag for flag in flags if flag in ('-DNDEBUG', '-UNDEBUG')][-1] == '-UNDEBUG'
        assert '-DOTELC_HAVE_LLVM_FIXTURES=1' in flags
        for field, name in [('product', 'native-control-test'),
                            ('function_product', 'native-function-test'),
                            ('object_product', 'native-object-test')]:
            assert digest(archive.read(name)) == record[field]['sha256']
        for name in ('native-object-test', 'native-control.profdata'):
            path = analysis / name
            with path.open('xb') as stream:
                stream.write(archive.read(name))
            path.chmod(0o600)
        command = [str(COV), 'export', str(analysis / 'native-object-test'),
                   '-instr-profile=' + str(analysis / 'native-control.profdata')]
        exported = subprocess.check_output(command, stderr=subprocess.PIPE)
        (analysis / 'llvm-export.json').write_bytes(exported)
        observed = json.loads(exported)
        floors = {}
        for block in observed['data']:
            for row in block['files']:
                if row['filename'] in record['line_coverage']:
                    measured = row['summary']['lines']
                    assert measured == record['line_coverage'][row['filename']]
                    assert measured['count'] > 0 and measured['percent'] >= 80
                    floors[row['filename']] = measured
        assert len(floors) == 3
        physical = {}
        for section in archive.read('native-control.lcov').decode().split('end_of_record'):
            rows = section.strip().splitlines()
            if not rows:
                continue
            names = [row[3:] for row in rows if row.startswith('SF:')]
            assert len(names) == 1
            name, = names
            assert name in floors
            counts = {}
            for row in rows:
                if row.startswith('DA:'):
                    line, count = map(int, row[3:].split(',')[:2])
                    assert line not in counts and line > 0 and count >= 0
                    counts[line] = count
            hit = sum(count > 0 for count in counts.values())
            assert counts and 100 * hit / len(counts) >= 80
            physical[name] = {'physical_lines': len(counts), 'physical_covered': hit,
                              'physical_percent': 100 * hit / len(counts)}
        assert len(physical) == 3
        methods = {}
        for block in observed['data']:
            for function in block['functions']:
                if 'FunctionRuntime' not in function['name']:
                    continue
                for name in ('AttachContext', 'DetachContext', 'ContextStatus', 'CurrentContext'):
                    if name in function['name']:
                        assert function['count'] > 0
                        methods[name] = {'mangled_name': function['name'], 'executions': function['count']}
        assert len(methods) == 4
        python = json.loads(archive.read('native-context/python-coverage.json'))
        assert all(row['summary']['percent_covered'] >= 80 for row in python['files'].values())
    assert hold(manifest) == held
    return {'status': 'pass for exact-head Linux SDK component', 'head': HEAD,
            'run': RUN, 'job': JOB, 'artifact': meta,
            'archive_sha256': digest(archive_bytes), 'source_facts_held': held,
            'source_inputs_bound': len(source_inputs), 'ctest_groups': 3,
            'owned_context_groups': 5, 'assertions_enabled': True,
            'native_file_floors': floors, 'raw_lcov_physical_floors': physical,
            'actual_new_api_executions': methods,
            'python_file_floors': {name: row['summary']['percent_covered']
                                   for name, row in python['files'].items()},
            'analysis_tool': {'path': str(COV), 'sha256': digest(COV.read_bytes()),
                              'version': subprocess.check_output([str(COV), '--version'], text=True).strip()},
            'local_analysis': 'Trusted LLVM reads non-executable retained object/profile bytes; no retained program is started.',
            'local_builds': 0, 'local_application_starts': 0,
            'limits': 'Runtime component only. Historical runner facts are data, not recertified. '
                      'Automatic hooks/public task or HTTP launch, macOS/full/Sonar/review, downstream OTLP, '
                      'oteleq and performance qualification remain separate.'}


if __name__ == '__main__':
    result = audit()
    result['controller_sha256'] = digest(Path(__file__).read_bytes())
    with (OUTPUT / 'linux-audit.json').open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'native_file_floors': result['native_file_floors'],
                      'new_api_executions': result['actual_new_api_executions']}))
