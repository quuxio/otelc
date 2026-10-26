"""Join the delivered artifact-scope fix to three reviewed main-target PRs."""
import json
import os
from pathlib import Path
import subprocess

BASE = Path('/Users/sclarke/github/otelc-qualification')
REPO = Path('/Users/sclarke/github/otelc')
MAIN = 'a1695fb6a6022db957b54bd37d77c920ea392c24'
FILES = ['.github/workflows/ci.yml', '.github/workflows/native-benchmarks.yml']
PROFILES = [
    (248, 'python-exceptions', 'e7b5b0102cbaac54269ed343567476e0c47bdb8f', 'fix/python-cancellation-identity',
     ['test_python_cancellation.CancellationClassificationTests', 'test_python_exception_names.ExceptionNameTests'], 4),
    (273, 'native-context', '8be4307d0eb025717908faf2b321af1561e09bbf', 'feat/native-owned-task-context', [], 0),
    (279, 'python-rng', '1c2ddd01aaa6d4bb27d8c3daed5a2e212f7267ce', 'test/python-public-random-state',
     ['test_python_random_state.RetryCaptureTests'], 2),
]
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 'quuxio', 'GIT_COMMITTER_NAME': 'quuxio',
       'GIT_AUTHOR_EMAIL': '312538871+quuxio@users.noreply.github.com',
       'GIT_COMMITTER_EMAIL': '312538871+quuxio@users.noreply.github.com'}


def git(root, *arguments):
    return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()


def main():
    rows = json.loads((BASE / '2026-10-26/current-gates-main-refresh.json').read_text())
    heads = {row['number']: row['headRefOid'] for row in rows}
    results = []
    for number, role, source, remote, tests, count in PROFILES:
        assert heads[number] == source
        root = BASE / ('work/' + role + '-main-a169')
        out = BASE / ('2026-10-26/' + role + '-main-a169')
        assert not root.exists() and not out.exists()
        out.mkdir(mode=0o700)
        subprocess.run(['git', 'worktree', 'add', '-b', 'chore/' + role + '-main-a169', str(root), source],
                       cwd=REPO, check=True, capture_output=True)
        assert not git(root, 'status', '--porcelain')
        result = subprocess.run(['git', 'merge', '--no-commit', '--no-ff', MAIN], cwd=root,
                                env=ENV, capture_output=True)
        (out / 'merge.stdout').write_bytes(result.stdout)
        (out / 'merge.stderr').write_bytes(result.stderr)
        assert result.returncode == 0, result.stderr.decode()
        assert sorted(git(root, 'diff', '--cached', '--name-only').splitlines()) == FILES
        for name in FILES:
            assert (root / name).read_bytes() == subprocess.check_output(['git', 'show', MAIN + ':' + name], cwd=root)
        subprocess.run(['git', 'diff', '--cached', '--check'], cwd=root, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'chore(' + role + '): integrate delivered artifact scope from main'],
                       cwd=root, env=ENV, check=True, capture_output=True)
        head = git(root, 'rev-parse', 'HEAD')
        assert git(root, 'show', '-s', '--format=%P', head).split() == [source, MAIN]
        assert sorted(git(root, 'diff', '--name-only', source, head).splitlines()) == FILES
        checked = []
        if tests:
            for version in ('312', '314'):
                python = '/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-' + version + '/bin/python'
                env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root / 'tests')}
                completed = subprocess.run([python, '-B', '-m', 'unittest', *tests, '-v'],
                    cwd=root, env=env, capture_output=True, timeout=60)
                raw = completed.stdout + completed.stderr
                (out / (version + '.log')).write_bytes(raw)
                assert completed.returncode == 0 and ('Ran ' + str(count) + ' tests').encode() in raw and b'\nOK\n' in raw, raw.decode()
                checked.append({'python': version, 'methods': count, 'exit': 0})
        assert not git(root, 'status', '--porcelain')
        subprocess.run(['python3', '-B', str(BASE / 'tools/freeze-reviewed-root.py'),
                        str(root), str(out / 'source'), source], check=True)
        record = {'pr': number, 'role': role, 'root': str(root), 'head': head, 'parents': [source, MAIN],
                  'remote_branch': remote, 'changed_files': FILES, 'changed_bytes_exact_main': True,
                  'product_and_test_sources_unchanged': True, 'local_checks': checked,
                  'limits': 'No local native/compiler/SDK builds; current-head hosted and strict main checks remain required. Earlier source runtime proofs are predecessor-only.'}
        (out / 'integration.json').write_text(json.dumps(record, indent=2) + '\n')
        results.append(record)
    path = BASE / '2026-10-26/main-a169-integrations.json'
    path.write_text(json.dumps(results, indent=2) + '\n')
    print(path)


if __name__ == '__main__':
    main()
