"""Normally join main's identical ancestor tree without changing reviewed source."""
import json
import os
from pathlib import Path
import subprocess

BASE = Path('/Users/sclarke/github/otelc-qualification')
MAIN = '937beb140d1fcdfedd6709547e3e6bf2ee3cb2c1'
ANCESTOR = '9c7e4ada9b4646626291d37118ca5fbb935abc9d'
PROFILES = [('context', 'native-context-main-937', 'e0e0991d66723608ec2775cebb4cf2b28eb133a4'),
            ('scope', 'native-scope-main', '82fc30466299ef28ad83bc9bff1afc0b035c80e9')]

for name, folder, source in PROFILES:
    root = BASE / 'work' / folder
    out = BASE / ('2026-10-26/native-' + name + '-main-937')
    out.mkdir(exist_ok=True)
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    assert git('rev-parse', 'HEAD') == source
    assert not git('status', '--porcelain')
    assert git('rev-parse', MAIN + '^{tree}') == git('rev-parse', ANCESTOR + '^{tree}')
    subprocess.run(['git', 'merge-base', '--is-ancestor', ANCESTOR, source], cwd=root, check=True)
    result = subprocess.run(['git', 'merge', '--no-commit', '--no-ff', MAIN], cwd=root, capture_output=True)
    (out / 'merge.stdout').write_bytes(result.stdout)
    (out / 'merge.stderr').write_bytes(result.stderr)
    assert result.returncode in (0, 1)
    conflicts = git('diff', '--name-only', '--diff-filter=U').splitlines()
    if result.returncode:
        assert conflicts
    for path in conflicts:
        assert git('show', MAIN + ':' + path) == git('show', ANCESTOR + ':' + path)
        subprocess.run(['git', 'checkout', 'HEAD', '--', path], cwd=root, check=True)
        subprocess.run(['git', 'add', '--', path], cwd=root, check=True)
    tree = git('rev-parse', source + '^{tree}')
    assert git('write-tree') == tree
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'quuxio', 'GIT_COMMITTER_NAME': 'quuxio',
           'GIT_AUTHOR_EMAIL': '312538871+quuxio@users.noreply.github.com',
           'GIT_COMMITTER_EMAIL': '312538871+quuxio@users.noreply.github.com'}
    subprocess.run(['git', 'commit', '-m', 'chore(native): reconcile reviewed ' + name + ' with main'],
                   cwd=root, env=env, check=True)
    head = git('rev-parse', 'HEAD')
    assert git('show', '-s', '--format=%P', head).split() == [source, MAIN]
    assert git('rev-parse', 'HEAD^{tree}') == tree and not git('status', '--porcelain')
    value = {'head': head, 'parents': [source, MAIN], 'unchanged_reviewed_tree': tree,
             'main_matches_existing_ancestor': ANCESTOR, 'resolved_conflicts': conflicts,
             'scope': 'Actual normal merge with source-tree identity proof; not transfer of old checks to the new head.'}
    (out / 'integration.json').write_text(json.dumps(value, indent=2) + '\n')
    print(json.dumps(value))
