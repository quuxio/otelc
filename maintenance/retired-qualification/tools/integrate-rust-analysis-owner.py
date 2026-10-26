"""Carry the independently reviewed analysis fix through the frozen Rust stack."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

BASE = Path('/Users/sclarke/github/otelc-qualification')
REPO = Path('/Users/sclarke/github/otelc')
FIX = '794d2d55a4ba81683a5b99078301cffe27f87556'
FILES = ['ci/native_sonar.py', 'docs/llvm-backend.md', 'tests/test_native_sonar.py']
PROFILES = [
    (275, 'owned', '05dcd699834d930f537d26fca3de09aa9fca6aab', 'feat/rust-owned-build'),
    (276, 'prepared', '9193bf18abae6df589db2821097b09c6af2292ca', 'feat/rust-prepared-execution'),
    (277, 'receipt', '395db0337a15edb07aab42625df14105c001764b', 'test/rust-prepared-telemetry-receipts'),
    (278, 'caller', 'cf8fdb766b5683bd079548f72bdedade985987b6', 'test/rust-generated-callers'),
]
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 'quuxio', 'GIT_COMMITTER_NAME': 'quuxio',
       'GIT_AUTHOR_EMAIL': '312538871+quuxio@users.noreply.github.com',
       'GIT_COMMITTER_EMAIL': '312538871+quuxio@users.noreply.github.com'}


def git(root, *arguments):
    return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()


def main():
    reviewed = json.loads((BASE / '2026-10-26/open-pr-checkpoint.json').read_text())
    heads = {row['number']: row['headRefOid'] for row in reviewed}
    parent = FIX
    results = []
    for number, role, source, remote_branch in PROFILES:
        assert heads[number] == source
        root = BASE / ('work/rust-' + role + '-analysis-owner')
        out = BASE / ('2026-10-26/rust-' + role + '-analysis-owner')
        assert not root.exists() and not out.exists()
        out.mkdir(mode=0o700)
        subprocess.run(['git', 'worktree', 'add', '-b', 'fix/rust-' + role + '-analysis-owner', str(root), source],
                       cwd=REPO, check=True, capture_output=True)
        assert not git(root, 'status', '--porcelain')
        merge = subprocess.run(['git', 'merge', '--no-commit', '--no-ff', parent], cwd=root,
                               env=ENV, capture_output=True)
        (out / 'merge.stdout').write_bytes(merge.stdout)
        (out / 'merge.stderr').write_bytes(merge.stderr)
        assert merge.returncode == 0, merge.stderr.decode()
        assert not git(root, 'diff', '--name-only', '--diff-filter=U')
        assert sorted(git(root, 'diff', '--cached', '--name-only').splitlines()) == FILES
        for name in FILES:
            expected = subprocess.check_output(['git', 'show', FIX + ':' + name], cwd=root)
            assert (root / name).read_bytes() == expected, name
        subprocess.run(['git', 'diff', '--cached', '--check'], cwd=root, capture_output=True, check=True)
        subprocess.run(['git', 'commit', '-m', 'fix(ci): integrate reviewed native analysis owners into Rust ' + role],
                       cwd=root, env=ENV, capture_output=True, check=True)
        head = git(root, 'rev-parse', 'HEAD')
        assert git(root, 'show', '-s', '--format=%P', head).split() == [source, parent]
        assert sorted(git(root, 'diff', '--name-only', source, head).splitlines()) == FILES
        assert not git(root, 'status', '--porcelain')
        result = {'pr': number, 'role': role, 'head': head, 'parents': [source, parent],
                  'root': str(root), 'remote_branch': remote_branch, 'changed_files': FILES,
                  'changed_files_byte_exact_reviewed': FIX, 'product_sources_unchanged': True,
                  'checks': []}
        for version in ('312', '314'):
            python = '/Users/sclarke/github/oteleq-qualification/2026-10-24/python-live-metrics/env-' + version + '/bin/python'
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(root / 'tests')}
            test = subprocess.run([python, '-B', '-m', 'unittest', 'test_native_sonar', '-v'],
                                  cwd=root, env=env, capture_output=True, timeout=30)
            raw = test.stdout + test.stderr
            (out / (version + '.log')).write_bytes(raw)
            assert test.returncode == 0 and b'Ran 12 tests' in raw and b'\nOK\n' in raw, raw.decode()
            result['checks'].append({'python': version, 'methods': 12, 'exit': 0})
        subprocess.run(['/opt/homebrew/bin/markdownlint', 'docs/llvm-backend.md'], cwd=root,
                       capture_output=True, check=True)
        subprocess.run(['python3', '-B', str(BASE / 'tools/freeze-reviewed-root.py'),
                        str(root), str(out / 'source'), source], check=True)
        (out / 'integration.json').write_text(json.dumps(result, indent=2) + '\n')
        results.append(result)
        parent = head
    final = BASE / '2026-10-26/rust-analysis-stack-integrations.json'
    final.write_text(json.dumps(results, indent=2) + '\n')
    print(final, hashlib.sha256(final.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
