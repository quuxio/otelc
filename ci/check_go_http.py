"""Check the owned HTTP glue against its reproducible official module product."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile

from ci import build_go_upstream as tooling
from otelc import go_http_bundle

ROOT = Path(__file__).resolve().parents[1]


def coverage(profile: Path) -> dict[str, float]:
    """Each maintained production file needs real nonempty statement coverage."""
    expected = {p.name for p in (ROOT / 'adapters/go-http').glob('*.go')
                if not p.name.endswith('_test.go') and p.name != 'http_bridge.go'}
    total: dict[str, int] = {}
    hit: dict[str, int] = {}
    for row in profile.read_text().splitlines()[1:]:
        position, size, count = row.split()
        name = position.split(':')[0].removeprefix('io.quux.otelc/go-http/')
        if name not in expected or int(size) < 0 or int(count) < 0:
            raise ValueError('unexpected Go HTTP coverage input')
        total[name] = total.get(name, 0) + int(size)
        hit[name] = hit.get(name, 0) + (int(size) if int(count) > 0 else 0)
    if set(total) != expected or any(not n for n in total.values()):
        raise ValueError('Go HTTP coverage is missing a production file')
    values = {name: 100 * hit[name] / size for name, size in total.items()}
    if any(value < 80 for value in values.values()):
        raise ValueError('Go HTTP production file is below80% statement coverage')
    return values


def check() -> dict:
    """Use a private modfile; never rewrite the maintained module or lockfiles."""
    tools = tooling.build('go', ROOT / 'build/go-upstream')
    environment = tooling.compiler_environment(tools, ROOT / 'build/go-upstream')
    go = tools['go']
    source = ROOT / 'adapters/go-http'
    output = ROOT / 'build/go-http-coverage.out'
    with tempfile.TemporaryDirectory(prefix='go-http-check-', dir=ROOT / 'build') as temporary:
        root = Path(temporary)
        scratch = root / 'compiler-temp'
        scratch.mkdir()
        environment.update(GOTMPDIR=str(scratch), TMPDIR=str(scratch))
        upstream = root / 'otelhttp'
        product = go_http_bundle.prepare(tools, upstream)
        modfile = root / 'go.mod'
        modfile.write_bytes((source / 'go.mod').read_bytes())
        commands = [[go, 'mod', 'edit', '-modfile=' + str(modfile),
            '-replace=io.quux.otelc/go=' + str(ROOT / 'adapters/go'),
            '-replace=' + go_http_bundle.MODULE + '=' + str(upstream)],
            [go, 'mod', 'tidy', '-modfile=' + str(modfile)],
            [go, 'vet', '-mod=readonly', '-modfile=' + str(modfile), './...'],
            [go, 'test', '-mod=readonly', '-modfile=' + str(modfile), '-race', '-count=1',
             '-coverprofile=' + str(output), './...']]
        formatted = subprocess.check_output([str(Path(go).parent / 'gofmt'), '-l',
                                             *map(str, source.glob('*.go'))], text=True)
        if formatted:
            raise ValueError('Go HTTP sources need gofmt: ' + formatted)
        for command in commands:
            subprocess.run(command, cwd=source, env=environment, check=True, timeout=300)
    result = {'go_version': tools['go_version'], 'official_module': product,
              'component_statement_percent': coverage(output)}
    (ROOT / 'build/go-http-check.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    print(json.dumps(check(), indent=2))
