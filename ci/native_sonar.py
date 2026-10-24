"""Bind actual C++ build context and LLVM line coverage for Sonar analysis."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from otelc import llvm_backend

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ('native/llvm/OtelcPass.cpp', 'adapters/native/src/metric_control.cpp')
TEST = 'adapters/native/tests/metric_control_test.cpp'


def add_line_record(raw: str, lines: dict[int, bool]) -> None:
    """Retain one physical line without merging duplicate report entries."""
    values = raw.split(',')
    if len(values) not in (2, 3):
        raise ValueError('invalid native LCOV line record')
    number, count = int(values[0]), int(values[1])
    if number <= 0 or count < 0 or number in lines:
        raise ValueError('invalid or repeated native LCOV line')
    lines[number] = count > 0


def lcov_lines(text: str, source: Path, *, summary: dict[str, Any] | None = None) -> dict[int, bool]:
    """Require one exact source record with complete, consistent line totals."""
    lines: dict[int, bool] = {}
    filenames, ends = [], 0
    totals = {}
    for line in text.splitlines():
        if line.startswith('SF:'):
            filenames.append(line[3:])
        elif line.startswith('DA:'):
            add_line_record(line[3:], lines)
        elif line.startswith(('LF:', 'LH:')):
            key, value = line.split(':')
            if key in totals:
                raise ValueError('repeated native LCOV total')
            totals[key] = int(value)
        elif line == 'end_of_record':
            ends += 1
    # LLVM emits file-summary totals separately from physical DA line records.
    # Expanded regions can make those denominators differ in the same export.
    expected = {'LF': len(lines), 'LH': sum(lines.values())} if summary is None else {
        'LF': summary['count'], 'LH': summary['covered']}
    if (filenames != [str(source)] or ends != 1 or not lines or totals != expected
            or not 0 <= expected['LH'] <= expected['LF'] or expected['LF'] <= 0
            or max(lines) > len(source.read_text().splitlines())):
        raise ValueError('native LCOV source or line denominator differs')
    if (100 * expected['LH'] / expected['LF'] < 80
            or 100 * sum(lines.values()) / len(lines) < 80):
        raise ValueError('native Sonar source coverage is below 80%')
    return lines


def compiled_unit(row: Any, expected: set[Path]) -> Path | None:
    """Check one owned unit's existing context and literal command representation."""
    if type(row) is not dict or type(row.get('file')) is not str:
        raise ValueError('invalid native compilation database entry')
    directory = Path(row.get('directory', ''))
    source = directory / row['file']
    if source.resolve() not in expected:
        return None
    if (not directory.is_absolute() or not directory.is_dir()
            or directory.resolve(strict=True) != directory):
        raise ValueError('native compiler context is missing or aliased')
    arguments, command = row.get('arguments'), row.get('command')
    if not ((type(arguments) is list and bool(arguments)
             and all(type(item) is str and item and '\0' not in item for item in arguments)
             and command is None)
            or (type(command) is str and bool(command) and '\0' not in command and arguments is None)):
        raise ValueError('native compilation entry requires one literal command representation')
    return source.resolve(strict=True)


def compilation_database(paths: list[Path], expected: set[Path]) -> list[dict[str, Any]]:
    """Keep exact compiled entries; no recorded compiler command is executed."""
    selected = {}
    for path in paths:
        records = json.loads(path.read_bytes())
        if type(records) is not list:
            raise ValueError('native compilation database must be a list')
        for row in records:
            source = compiled_unit(row, expected)
            if source is None:
                continue
            if source in selected:
                raise ValueError('native compilation unit is duplicated')
            selected[source] = row
    if set(selected) != expected:
        raise ValueError('native compilation database must cover every exact translation unit')
    return [selected[path] for path in sorted(expected)]


def checked_artifact(row: dict[str, Any], path: Path) -> None:
    """Refuse stale coverage/context files rather than importing another build."""
    if row != llvm_backend.identity(path):
        raise ValueError('native Sonar input changed: ' + path.name)


def generate(root: Path = ROOT) -> None:
    """Convert current checked component reports and optionally keep scan context."""
    output = root / 'build'
    llvm = json.loads((output / 'llvm-pass-coverage-identity.json').read_bytes())
    native = json.loads((output / 'native-control-coverage.json').read_bytes())
    checked_artifact(llvm['source'], root / PRODUCTION[0])
    for row in native['source_inputs']:
        checked_artifact(row, Path(row['path']))
    native_source = llvm_backend.identity(root / PRODUCTION[1])
    if native_source not in native['source_inputs']:
        raise ValueError('native SDK coverage omitted its exact implementation input')
    document = ET.Element('coverage', version='1')
    databases = []
    for relative, record in zip(PRODUCTION, (llvm, native), strict=True):
        source = root / relative
        lcov = Path(record['lcov']['path'])
        database = Path(record['compilation_database']['path'])
        checked_artifact(record['lcov'], lcov)
        checked_artifact(record['compilation_database'], database)
        databases.append(database)
        file = ET.SubElement(document, 'file', path=relative)
        summary = record['line_coverage'] if relative == PRODUCTION[0] else record['line_coverage'][str(source)]
        for number, covered in sorted(lcov_lines(lcov.read_text(), source, summary=summary).items()):
            ET.SubElement(file, 'lineToCover', lineNumber=str(number), covered=str(covered).lower())
    ET.ElementTree(document).write(output / 'native-sonar-coverage.xml', encoding='utf-8', xml_declaration=True)
    if os.environ.get('OTELC_CPP_ANALYSIS') == '1':
        entries = compilation_database(databases, {root / path for path in (*PRODUCTION, TEST)})
        (output / 'native-compile-commands.json').write_text(json.dumps(entries, indent=2) + '\n')


if __name__ == '__main__':
    generate()
