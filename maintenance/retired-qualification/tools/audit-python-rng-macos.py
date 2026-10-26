"""Audit the retained exact-head macOS public CLI RNG run without extraction."""
import hashlib
import json
from pathlib import Path
import zipfile

OUT = Path('/Users/sclarke/github/otelc-qualification/2026-10-26/python-random-state-accounting')
HEAD = '1c2ddd01aaa6d4bb27d8c3daed5a2e212f7267ce'


def main():
    archive = OUT / 'macos-original.zip'
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == '8bd1eaaa4fa7f0e0887ddaa7271ce6caec7329d5ddb66b09e2974dbf45e6aaa7'
    rows = []
    with zipfile.ZipFile(archive) as saved:
        assert saved.testzip() is None
        assert saved.read('head.txt').decode().strip() == HEAD
        log = saved.read('tests.log')
        assert b'Ran 4 tests' in log and b'\nOK\n' in log
        names = saved.namelist()
        assert len(names) == len(set(names))
        for name in sorted(n for n in names if n.endswith('/result.json')):
            prefix = name[:-len('result.json')]
            root = prefix.rsplit('/', 2)[0] + '/'
            result = json.loads(saved.read(name))
            metrics = 'metrics-1-' in prefix
            traces = '-traces-1/' in prefix
            retry = 'real_http_retries/' in prefix
            source = saved.read(root + 'application/entry.py')
            config = saved.read(prefix + 'otelc.toml')
            assert result['source_sha256'] == result['source_after_sha256'] == hashlib.sha256(source).hexdigest()
            assert result['config_sha256'] == result['config_after_sha256'] == hashlib.sha256(config).hexdigest()
            assert result['stdout'].encode() == saved.read(root + 'baseline.stdout')
            assert result['stderr'] == '' and result['returncode'] == 0
            before, after = map(json.loads, result['stdout'].splitlines())
            assert before['values'] == [0.41661987254534116, 0.010169169457068361, 0.8252065092537432]
            assert before['state'] == after['state'] == '691d0ff3de4d6ee50af7a22d2c57dd709f2e7936c0b106d09d446979ed4ae5f4'
            paths = {path for enabled, path in ((metrics, '/v1/metrics'), (traces, '/v1/traces')) if enabled}
            assert set(result['responses']) == paths
            for path, statuses in result['responses'].items():
                assert statuses[0] == (503 if retry else 200)
                assert statuses.count(503) == int(retry)
                assert set(statuses) <= {200, 503} and 200 in statuses
                if path == '/v1/metrics':
                    assert statuses.count(200) >= 2
            execution = json.loads(saved.read(prefix + 'runtime/python-execution.json'))
            sdk = json.loads(saved.read(prefix + 'runtime/sdk/python-sdk-status.json'))
            assert execution['phase'] == sdk['phase'] == 'closed'
            assert execution['failures'] == {} and execution['input_recheck'] and execution['bootstrap_recheck']
            assert execution['application']['returncode'] == execution['sdk']['returncode'] == 0
            assert sdk['producer']['function_completions'] == 3
            assert sdk['producer']['active_functions'] == 0
            assert sdk['producer']['runtime_losses'] == {'incomplete': 0}
            assert sdk['closed'] and sdk['shutdown_complete'] and not sdk['shutdown_failed']
            assert sdk['span_accounting']['queue_pending'] == sdk['span_accounting']['queue_dropped'] == 0
            assert sdk['span_accounting']['received'] == sdk['span_accounting']['sdk_exported'] == (3 if traces else 0)
            assert sdk['protocol_errors'] == sdk['reply_lost'] == sdk['operation_failures'] == 0
            rows.append({'case': prefix, 'metrics': metrics, 'traces': traces, 'failed_first': retry,
                         'responses': result['responses'], 'rng_state': before['state']})
    assert len(rows) == 8
    value = {'head': HEAD, 'run': 40269279590, 'job': 121398377133, 'artifact': 12629316618,
             'host': 'macos', 'original_zip_sha256': digest,
             'original_log_sha256': hashlib.sha256((OUT / 'macos-original.log').read_bytes()).hexdigest(),
             'methods': 4, 'genuine_signal_transport_cases': 8, 'cases': rows,
             'scope': 'Exact-head CPython3.14 macOS public native CLI. Seeded output/state, original source/config and closed producer/SDK accounting with actual HTTP retries. Not whole-program state equivalence, original OTLP wire archive, 3.12 runtime proof or provider admission.'}
    target = OUT / 'macos-audit.json'
    target.write_text(json.dumps(value, indent=2) + '\n')
    print(target, hashlib.sha256(target.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
