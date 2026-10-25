"""Import exact-source Rust LCOV and Clippy output without executing report data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ci.native_sonar import lcov_lines, lcov_records
from otelc.file_inputs import identity

RUST_SOURCES = ('native/rust/compiler/src/main.rs', 'native/rust/compiler/src/request.rs',
                'native/rust/compiler/src/hooks.rs', 'native/rust/compiler/src/instrument.rs',
                'native/rust/bridge/src/lib.rs')


def source_paths(root: Path) -> list[Path]:
    """Keep new production modules from silently falling outside the gate."""
    rust = sorted((root / 'native/rust').rglob('*.rs'))
    tests = {root / 'native/rust/compiler/src/request_tests.rs'}
    if set(rust) - tests != {root / name for name in RUST_SOURCES}:
        raise ValueError('Rust coverage source catalogue changed')
    return [*rust, *sorted((root / 'native/rust').rglob('Cargo.toml')),
            *sorted((root / 'native/rust').rglob('Cargo.lock')),
            *(root / name for name in ('ci/rust_compiler_cases.py', 'ci/check_rust_compiler.py',
                                      'ci/rust_sonar.py', 'otelc/health.py'))]


def clippy_report(raw: bytes) -> None:
    """Require both successful Cargo runs; never accept an empty/failed import."""
    finished = 0
    for line in raw.splitlines():
        value = json.loads(line)
        if type(value) is not dict or type(value.get('reason')) is not str:
            raise ValueError('invalid Rust Clippy JSON record')
        if value['reason'] == 'build-finished':
            if value.get('success') is not True:
                raise ValueError('Rust Clippy did not finish successfully')
            finished += 1
        if value['reason'] == 'compiler-message':
            message = value.get('message')
            if type(message) is not dict:
                raise ValueError('invalid Rust Clippy diagnostic')
            if message.get('level') in ('warning', 'error'):
                raise ValueError('Rust Clippy warnings/errors must be fixed before importing')
    if finished != 2:
        raise ValueError('Rust Clippy requires both compiler and bridge results')


def write(root: Path, source_inputs: list[dict], lcov: str, summaries: dict,
          clippy: bytes, output: Path) -> None:
    """Validate measured sources before publishing repository-relative reports."""
    expected = {root / name for name in RUST_SOURCES}
    held = {row['path']: row for row in source_inputs}
    if any(str(path) not in held or identity(path) != held[str(path)] for path in expected):
        raise ValueError('Rust coverage does not bind the current source bytes')
    if set(summaries) != {str(path) for path in expected}:
        raise ValueError('Rust coverage summary must identify every production module')
    records = lcov_records(lcov, expected)
    relative = []
    for path, record in records.items():
        lcov_lines(record, path, summary=summaries[str(path)])
        relative.append(record.replace('SF:' + str(path) + '\n', 'SF:' + path.relative_to(root).as_posix() + '\n', 1))
    clippy_report(clippy)
    (output / 'rust-coverage.lcov').write_text(''.join(relative))
    (output / 'rust-clippy.json').write_bytes(clippy)
    files = {name: {'sha256': hashlib.sha256((root / name).read_bytes()).hexdigest(),
                    'summary': summaries[str(root / name)]} for name in RUST_SOURCES}
    (output / 'rust-analysis-inputs.json').write_text(json.dumps({'schema_version': 1,
        'sources': files, 'source_inputs': source_inputs,
        'reports': {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ('rust-coverage.lcov', 'rust-clippy.json')}}, indent=2) + '\n')


def verify(root: Path, output: Path) -> None:
    """Refuse stale sources, substituted reports and incomplete analysis records."""
    value = json.loads((output / 'rust-analysis-inputs.json').read_bytes())
    if (type(value) is not dict or set(value) != {'schema_version', 'sources', 'source_inputs', 'reports'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or any(type(value[key]) is not dict for key in ('sources', 'reports'))
            or value['source_inputs'] != [identity(path) for path in source_paths(root)]
            or set(value['sources']) != set(RUST_SOURCES)
            or set(value['reports']) != {'rust-coverage.lcov', 'rust-clippy.json'}):
        raise ValueError('Rust analysis source bindings changed')
    for name, expected in value['reports'].items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Rust analysis report changed')
    for name, row in value['sources'].items():
        if row['sha256'] != hashlib.sha256((root / name).read_bytes()).hexdigest():
            raise ValueError('Rust analysis source changed')
    # Recheck the closed relative coverage catalogue and its measured floor.
    text = (output / 'rust-coverage.lcov').read_text()
    records = lcov_records(text, {Path(name) for name in RUST_SOURCES})
    for name, record in records.items():
        absolute = root / name
        lcov_lines(record.replace('SF:' + name.as_posix() + '\n', 'SF:' + str(absolute) + '\n', 1),
                   absolute, summary=value['sources'][name.as_posix()]['summary'])
    clippy_report((output / 'rust-clippy.json').read_bytes())


if __name__ == '__main__':
    verify(Path(__file__).resolve().parents[1], Path(__file__).resolve().parents[1] / 'build')
