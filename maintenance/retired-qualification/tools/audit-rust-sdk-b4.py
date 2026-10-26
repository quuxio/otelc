"""Audit retained exact-head Rust SDK and public runtime evidence."""
from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile


HEAD = 'b4ecf3a428b4945d43ce01dce3c621deb54c1ea2'
BASE = Path('/Users/sclarke/github/otelc-qualification/2026-10-25/rust-types-suite-budget')
OUTPUT = Path('/Users/sclarke/github/otelc-qualification/2026-10-26/rust-types-suite-budget/hosted')
HELPER = ['/usr/bin/python3', '/Users/sclarke/github/otelc-qualification/tools/quux-remote.py', 'api']


def api(path: str) -> bytes:
    """Read GitHub through the existing identity-checked helper."""
    flags = ['--allow-escape-sequences'] if path.endswith('/logs') else []
    return subprocess.check_output([*HELPER, 'repos/quuxio/otelc/' + path, *flags])


def retain(path: Path, raw: bytes) -> None:
    """Keep an original response once, requiring equality on repeat reads."""
    if path.exists():
        assert path.read_bytes() == raw, str(path)
    else:
        with path.open('xb') as stream:
            stream.write(raw)


def audit(row: tuple[str, str, int, int]) -> dict:
    """Bind raw archive/logs to the source head and inspect actual results."""
    kind, host, artifact_id, job_id = row
    metadata = json.loads(api(f'actions/artifacts/{artifact_id}'))
    assert metadata['workflow_run']['head_sha'] == HEAD
    archive_path = OUTPUT / f'{kind}-{host}-original.zip'
    raw = archive_path.read_bytes() if archive_path.exists() else api(f'actions/artifacts/{artifact_id}/zip')
    digest = hashlib.sha256(raw).hexdigest()
    assert metadata['size_in_bytes'] == len(raw)
    assert metadata['digest'] == 'sha256:' + digest
    retain(archive_path, raw)
    job = json.loads(api(f'actions/jobs/{job_id}'))
    assert job['run_id'] == metadata['workflow_run']['id'] == 40245266436
    assert job['status'] == 'completed' and job['conclusion'] == 'success', job
    log_path = OUTPUT / f'{kind}-{host}-original.log.gz'
    if log_path.exists():
        log = gzip.decompress(log_path.read_bytes())
    else:
        log = api(f'actions/jobs/{job_id}/logs')
        retain(log_path, gzip.compress(log, mtime=0))
    record = {'head': HEAD, 'job': job_id, 'conclusion': job['conclusion'],
              'artifact': metadata, 'archive_sha256': digest,
              'original_log_sha256': hashlib.sha256(log).hexdigest()}
    with zipfile.ZipFile(archive_path) as archive:
        if kind == 'sdk':
            names = archive.namelist()
            result_name = next(name for name in names if name.endswith('/result.json') and name.count('/') == 1)
            result = json.loads(archive.read(result_name))
            tests = json.loads(archive.read(next(name for name in names if name.endswith('/tests-result.json'))))
            assert tests['successful'] and tests['failures'] == tests['errors'] == tests['skipped'] == 0
            assert tests['selected'] == tests['tests_run'] == 40
            assert result['application_processes'] == 24
            assert result['automatic_lifetime_application_processes'] == 8
            coverage = {**result['compiler_line_coverage'], **result['bridge_line_coverage']}
            assert len(coverage) == 7 and all(value['percent'] >= 80 for value in coverage.values())
            sections = archive.read('rust-coverage.lcov').decode().split('end_of_record')
            independent = {}
            for section in sections:
                rows = section.strip().splitlines()
                if not rows: continue
                names = [line[3:] for line in rows if line.startswith('SF:')]
                assert len(names) == 1
                source_name, = names
                counts = {}
                for line in rows:
                    if line.startswith('DA:'):
                        line_number, count = map(int, line[3:].split(',')[:2])
                        assert line_number > 0 and count >= 0 and line_number not in counts
                        counts[line_number] = count
                total, hit = len(counts), sum(count > 0 for count in counts.values())
                assert total and 100 * hit / total >= 80
                matches = [name for name in coverage if name.endswith('/' + source_name)]
                assert len(matches) == 1
                measured = coverage[matches[0]]
                assert [line for line in rows if line.startswith('LF:')] == ['LF:' + str(measured['count'])]
                assert [line for line in rows if line.startswith('LH:')] == ['LH:' + str(measured['covered'])]
                assert 100 * measured['covered'] / measured['count'] >= 80
                exported = []
                for suffix in ('compiler-coverage.stdout', 'bridge-coverage.stdout'):
                    name, = [n for n in archive.namelist() if n.endswith('/' + suffix)]
                    exported.extend(f for block in json.loads(archive.read(name))['data'] for f in block['files']
                                    if f['filename'] == matches[0])
                assert len(exported) == 1 and exported[0]['summary']['lines'] == measured
                independent[source_name] = {'physical_da_count': total, 'physical_da_covered': hit,
                    'physical_da_percent': 100 * hit / total,
                    'llvm_export_summary': measured,
                    'note': 'Physical DA and LLVM file-summary denominators remain distinct; both independently exceed80%.'}
            assert len(independent) == 7
            record['independently_recomputed_raw_lcov'] = independent
            record['audit_correction'] = 'Initial reader assumed LF/LH equal physical DA counts; existing native_sonar explicitly treats LLVM expanded-summary denominator separately. No SDK/product change; both physical DA and raw LLVM-export summary floors independently checked.'
            source = json.loads((BASE / 'source.json').read_bytes())
            for item in result['source_inputs']:
                matches = [name for name, facts in source['source_facts'].items()
                           if item['path'].endswith('/' + name) and item['sha256'] == facts[0]]
                assert len(matches) == 1, item['path']
            record.update(tests=tests, application_processes=24, automatic_lifetime_processes=8,
                          coverage=coverage, source_inputs_match_frozen_source=len(result['source_inputs']),
                          scope='Compiler/private lifetime corpus only; no public lifetime admission')
        else:
            assert archive.read('head.txt').decode().strip() == HEAD
            text = archive.read('tests.log').decode()
            match = re.search(r'Ran (\d+) tests in ([\d.]+)s', text)
            assert match and int(match[1]) == 81
            assert re.search(r'\nOK(?: \(skipped=1\))?\n', text)
            health = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('/health.json')]
            assert len(health) == 13
            assert all(value['schema_version'] == 1 and 'drop_accounting' not in value for value in health)
            reports = [json.loads(archive.read(name)) for name in archive.namelist() if name.endswith('/report.json')]
            assert len(reports) == 5
            for report in reports:
                assert report['language'] == 'rust' and report['iterations'] == 3
                assert len(report['transitions']) == 8
                assert {item['pid'] for item in report['transitions']} == {report['pids']['instrumented']}
                assert set(report['samples_ns_per_operation']) == {'plain', 'both_off', 'both_on', 'collection_only', 'emission_only'}
                assert all(len(samples) == 2 for samples in report['samples_ns_per_operation'].values())
            coverage = json.loads(archive.read('python-coverage.json'))['files']
            assert len(coverage) == 5 and all(value['summary']['percent_covered'] >= 80 for value in coverage.values())
            record.update(tests=81, skipped=int('(skipped=1)' in text), seconds=float(match[2]), health_reports=13,
                          benchmark_reports=5, transitions_per_report=8,
                          coverage_percent={name: value['summary']['percent_covered'] for name, value in coverage.items()},
                          scope='Public function-only runtime; two samples/three iterations are smoke checks, not performance qualification or public lifetimes')
    retain(OUTPUT / f'{kind}-{host}-audit.json', (json.dumps(record, indent=2) + '\n').encode())
    print(json.dumps({'kind': kind, 'host': host, 'tests': record['tests'], 'artifact': artifact_id}), flush=True)
    return record


if __name__ == '__main__':
    OUTPUT.mkdir(parents=True, exist_ok=True)
    rows = [('sdk', 'linux', 12618965718, 121325377324),
            ('sdk', 'macos', 12618703475, 121325377154)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(audit, rows))
