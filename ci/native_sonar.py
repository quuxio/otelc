"""Bind actual C++ build context and LLVM line coverage for Sonar analysis."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from otelc import llvm_backend

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ('native/llvm/OtelcPass.cpp', 'adapters/native/src/metric_control.cpp',
              'adapters/native/src/function_runtime.cpp', 'adapters/native/launcher/src/runtime.cpp',
              'adapters/native/gate/ExecGate.c', 'native/clang/Lifetimes.cpp', 'adapters/native/src/object_runtime.cpp',
              'native/llvm/ObjectPass.cpp', 'native/clang/Resources.cpp', 'native/llvm/ResourcePass.cpp')
CONTROL_PRODUCTION = (PRODUCTION[1], PRODUCTION[2], PRODUCTION[6])
TEST = 'adapters/native/tests/metric_control_test.cpp'
TESTS = (TEST, 'adapters/native/tests/function_runtime_test.cpp',
         'adapters/native/tests/fixtures/functions.c', 'adapters/native/tests/fixtures/functions.cpp',
         'adapters/native/tests/object_runtime_test.cpp', 'tests/fixtures/native-objects.cpp')


def lcov_records(text: str, expected: set[Path]) -> dict[Path, str]:
    """Separate the complete exact source denominator before checking each floor."""
    records, current = {}, []
    for line in text.splitlines():
        current.append(line)
        if line == 'end_of_record':
            names = [Path(row[3:]) for row in current if row.startswith('SF:')]
            if len(names) != 1 or names[0] not in expected or names[0] in records:
                raise ValueError('native LCOV contains a foreign or duplicated source')
            records[names[0]] = '\n'.join(current) + '\n'
            current = []
    if any(row.strip() for row in current) or set(records) != expected:
        raise ValueError('native LCOV must cover every complete implementation record')
    return records


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
    llvm = json.loads((output / 'llvm-pass-source-coverage-identity.json').read_bytes())
    native = json.loads((output / 'native-control-coverage.json').read_bytes())
    launch = json.loads((output / 'native-launch-coverage.json').read_bytes())
    gate = json.loads((output / 'native-gate-coverage.json').read_bytes())
    lifetimes = json.loads((output / 'native-lifetimes-coverage.json').read_bytes())
    objects = json.loads((output / 'native-object-pass-coverage.json').read_bytes())
    resources = json.loads((output / 'native-resources-coverage.json').read_bytes())
    resource_pass = json.loads((output / 'native-resource-pass-coverage.json').read_bytes())
    for record, index in ((resources, 8), (resource_pass, 9)):
        checked_artifact(record['source'], root / PRODUCTION[index])
        for header in record.get('headers', []):
            checked_artifact(header, Path(header['path']))
    checked_artifact(objects['source'], root / PRODUCTION[7])
    for header in objects['headers']:
        checked_artifact(header, Path(header['path']))
    checked_artifact(llvm['source'], root / PRODUCTION[0])
    for header in llvm['headers']:
        checked_artifact(header, Path(header['path']))
    checked_artifact(gate['source'], root / PRODUCTION[4])
    checked_artifact(lifetimes['source'], root / PRODUCTION[5])
    for record, relatives in ((native, CONTROL_PRODUCTION), (launch, PRODUCTION[3:4])):
        for row in record['source_inputs']:
            checked_artifact(row, Path(row['path']))
        if any(llvm_backend.identity(root / relative) not in record['source_inputs'] for relative in relatives):
            raise ValueError('native SDK coverage omitted its exact implementation input')
    checked_artifact(native['lcov'], Path(native['lcov']['path']))
    checked_artifact(launch['lcov'], Path(launch['lcov']['path']))
    native_records = lcov_records(Path(native['lcov']['path']).read_text(), {root / path for path in CONTROL_PRODUCTION})
    native_records.update(lcov_records(Path(launch['lcov']['path']).read_text(), {root / PRODUCTION[3]}))
    checked_artifact(gate['lcov'], Path(gate['lcov']['path']))
    native_records.update(lcov_records(Path(gate['lcov']['path']).read_text(), {root / PRODUCTION[4]}))
    document = ET.Element('coverage', version='1')
    databases = []
    for relative, record in zip(PRODUCTION, (llvm, native, native, launch, gate, lifetimes, native, objects,
                                            resources, resource_pass), strict=True):
        source = root / relative
        lcov = Path(record['lcov']['path'])
        database = Path(record['compilation_database']['path'])
        checked_artifact(record['lcov'], lcov)
        checked_artifact(record['compilation_database'], database)
        if database not in databases:
            databases.append(database)
        file = ET.SubElement(document, 'file', path=relative)
        single = relative in (PRODUCTION[0], PRODUCTION[5], PRODUCTION[7], PRODUCTION[8], PRODUCTION[9])
        summary = record['line_coverage'] if single else record['line_coverage'][str(source)]
        text = lcov.read_text() if single else native_records[source]
        for number, covered in sorted(lcov_lines(text, source, summary=summary).items()):
            ET.SubElement(file, 'lineToCover', lineNumber=str(number), covered=str(covered).lower())
    ET.ElementTree(document).write(output / 'native-sonar-coverage.xml', encoding='utf-8', xml_declaration=True)
    if os.environ.get('OTELC_CPP_ANALYSIS') == '1':
        entries = compilation_database(databases, {root / path for path in (*PRODUCTION, *TESTS)})
        (output / 'native-compile-commands.json').write_text(json.dumps(entries, indent=2) + '\n')


if __name__ == '__main__':
    generate()
