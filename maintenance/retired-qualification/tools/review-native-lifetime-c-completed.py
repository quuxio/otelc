"""Independently audit d1b1a0e lifetime shard bytes and fresh refusal controls."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile


BASE = Path('/Users/sclarke/github/oteleq-qualification')
BUILD = BASE / '2026-10-26/native-lifetime-d1b1a0e'
SOURCE = BASE / 'work/oteleq-native-error-contract'
RUNNER = Path('/home/runner/work/_temp/native-lifetime-evidence')
HEAD = 'd1b1a0ef8fa2ae1fb38cc5e7a69d18d53cb45805'
sys.path.insert(0, str(SOURCE / 'tests'))
import qualify_native_lifetimes as driver
import native_lifetime_audit as raw
import native_lifetime_refusals as refusals
import native_prepared_receipt as records


def digest(path: Path) -> str:
    """Read a complete retained regular file."""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_archives(root: Path, original: dict) -> dict:
    """Match every archived source byte to independent local Git objects."""
    facts = records.read(root / 'original-source-facts.json')
    result = {}
    for role, repo, head, count, expected_tar in (
        ('consumer', SOURCE, HEAD, 685,
         'c58722a17201a2a5dba56ca2ba7483730cba06b841d00d85ebcfffdac7a879d9'),
        ('provider', Path('/Users/sclarke/github/otelc-qualification/work/rust-drop-engine'),
         driver.SDK_HEAD, 503,
         '39b932e83dce61c415652d828992843a998e505debfbc77d7e712754b3d2ff5d'),
    ):
        catalogue = facts['/home/runner/work/oteleq/oteleq/' + role]
        tree = {}
        rows = subprocess.check_output(['git', 'ls-tree', '-rz', '--full-tree', head], cwd=repo)
        for row in rows.rstrip(b'\0').split(b'\0'):
            metadata, name = row.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            assert kind == 'blob'
            tree[name.decode()] = (mode, oid)
        assert len(tree) == count and set(tree) == set(catalogue)
        archive = root / (role + '-source.tar')
        assert digest(archive) == expected_tar
        seen = set()
        with tarfile.open(archive) as bundle:
            for member in bundle:
                if member.isdir():
                    continue
                assert member.isfile() and member.name in tree and member.name not in seen
                stream = bundle.extractfile(member)
                assert stream is not None
                body = stream.read()
                mode, oid = tree[member.name]
                assert hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest() == oid
                assert hashlib.sha256(body).hexdigest() == catalogue[member.name]['sha256']
                assert len(body) == catalogue[member.name]['size']
                assert member.mode & 0o111 == int(mode, 8) & 0o111
                seen.add(member.name)
        assert seen == set(tree)
        assert (root / (role + '-source-status.txt')).read_bytes() == b''
        result[role] = {'head': head, 'files': len(seen), 'tar_sha256': expected_tar}
    return result


class HistoricalAudit:
    """Join literal observations to original launch records and image bytes."""

    def __init__(self, root: Path, bundle: zipfile.ZipFile, original: dict, name: str):
        self.root, self.bundle, self.original, self.name = root, bundle, original, name
        self.mode = driver.CASE_MODES[name]
        self.seen: dict = {}
        self.contexts: dict = {}
        self.observations = 0
        self.copied = 0
        self.images: dict = {}
        self.totals = Counter()

    def archived_fact(self, fact: dict, path: Path) -> str:
        """Hash a fixed expected archive member; historical stat values stay data."""
        records.same(fact['path'], str(path))
        records.same(fact['resolved_path'], str(path))
        member = 'native-lifetime-evidence/' + path.relative_to(RUNNER).as_posix()
        info = self.bundle.getinfo(member)
        assert fact['size'] == info.file_size
        with self.bundle.open(info) as stream:
            value = hashlib.file_digest(stream, 'sha256').hexdigest()
        records.same(fact['sha256'], value)
        return value

    def attempt(self, report: Path, folder: Path, entry: dict, lane: str, witness,
                expected_arguments: tuple[int, ...], *, function: str = 'selected',
                mode: str = 'completed') -> None:
        records.same(mode, self.mode)
        request = records.read(folder / 'native-runtime/native-request.json')
        launch = request['launch_id']
        assert type(launch) is str and re.fullmatch('[a-f0-9]{32}', launch)
        row = self.original['audit']['records'][launch]
        assert set(row) == {'pid', 'folder', 'plain', 'mode', 'raw_lifetime_receipt'}
        original_folder = self.root / Path(row['folder']).relative_to(RUNNER)
        assert original_folder.is_relative_to(self.root / self.name)
        plain = lane == 'baseline'
        records.same(request['plain'], plain)
        records.same(request['runtime'], str(Path(row['folder']) / 'native-runtime'))
        records.same(row['plain'], plain)
        records.same(row['mode'], mode)
        application = records.read(folder / 'native-runtime/native-application-start.json')
        finished = records.read(folder / 'native-runtime/oteleq-native-finish.json')
        caller = records.read(folder / 'native-caller/caller.json')
        records.same(caller['entry'], entry)
        records.same(driver.arguments(caller['arguments']), expected_arguments)
        records.same(application['launch_id'], launch)
        records.same(application['pid'], row['pid'])
        records.same(application['parent_pid'], application['supervisor_pid'])
        records.same(application['process_group'], application['pid'])
        records.same(application['session'], application['pid'])
        records.same(finished['application_pid'], application['pid'])
        records.same(finished['sdk_pid'], None if plain else application['pid'])
        records.same(finished['supervisor_pid'], application['supervisor_pid'])
        records.same(finished['application_exit_code'], 0)
        prefix = Path(row['folder']) / 'native-build'
        image = self.archived_fact(application['image'], prefix / ('plain' if plain else 'instrumented'))
        self.archived_fact(request['build'], prefix / 'native-source-build.json')
        self.images[launch] = image
        observed = raw.verify(folder, entry, witness, plain=plain, function=function, mode=mode)
        records.same(observed, row['raw_lifetime_receipt'])
        records.same(self.seen.setdefault(launch, observed), observed)
        self.observations += 1
        if folder != original_folder:
            records.same(folder.relative_to(self.root).parts[:3], (self.name, 'shrinking', 'original'))
            self.copied += 1
        elif not plain and launch not in self.contexts:
            self.contexts[launch] = (folder, entry, witness, function)
            self.totals.update({key: observed[key] for key in
                                ('spans', 'metric_points', 'trace_requests', 'metric_requests')})


def main(name: str) -> None:
    """Audit one explicitly successful current-head shard without application runs."""
    selected, = driver.select_cases([name])
    _, language, difference = selected
    mode = driver.CASE_MODES[name]
    index = records.read(BUILD / (name + '-selected-index-01.json'))
    records.same(index['head'], HEAD)
    records.same(index['case'], name)
    archive = Path(index['archive'])
    assert digest(archive) == index['archive_sha256']
    selected_root = Path(index['root'])
    root = selected_root / 'native-lifetime-evidence'
    out = Path('/Users/sclarke/github/otelc-qualification/2026-10-26/oteleq-c-completed-d1-peer-review/replay-01')
    out.mkdir(mode=0o700)
    (out / 'controller.py').write_bytes(Path(__file__).read_bytes())
    held = {}
    for member, fact in index['files'].items():
        path = selected_root / member
        assert digest(path) == fact['sha256'] and path.stat().st_size == fact['bytes']
        held[str(path)] = records.identity(path)
    original = records.read(root / 'qualification.json')
    records.same(original['consumer_source'], HEAD)
    records.same(original['sdk_source'], driver.SDK_HEAD)
    records.same(original['selected_cases'], [name])
    records.same(original['complete_corpus'], False)
    assert len(original['cases']) == 1
    case = original['cases'][0]
    records.same({key: case[key] for key in ('name', 'language', 'difference', 'mode')},
                 {'name': name, 'language': language, 'difference': difference, 'mode': mode})
    routes = ['generated', 'framework', 'replay', 'detached']
    if difference:
        routes.append('scalar-reduction')
    routes.append('current-source-promotion')
    records.same(original['routes'], routes)
    expected_launches = 28 + 16 * difference
    records.same(original['actual_application_launches'], expected_launches)
    records.same(original['audit']['actual_application_records'], expected_launches)
    records.same(original['audit']['raw_lifetime_receipts'], expected_launches // 2)
    assert len(original['audit']['records']) == expected_launches
    catalogues = source_archives(root, original)
    promotion = case['promotion']
    records.same(promotion['original_calls'], dict(driver.BASE_CALLS))
    records.same(promotion['changed_calls'], dict(driver.PROMOTED_CALLS))
    records.same(promotion['application_observations'], 12)
    records.same(promotion['fresh_current_source'], True)
    records.same(promotion['relocated_compilation_database'], True)
    assert len(promotion['reports']) == 3 and len(set(promotion['reports'])) == 3
    promoted = {}
    for position, path in enumerate(promotion['reports']):
        relative = Path(path).relative_to(RUNNER)
        records.same(relative.parts[:3], (name, 'promotion', 'current-workspaces'))
        promoted[root / relative] = driver.BASE_CALLS if position == 0 else driver.PROMOTED_CALLS
    expected_reports = (expected_launches + 4 * difference) // 4
    reports = sorted(root.rglob('report.json'))
    assert len(reports) == expected_reports
    products = records.read(root / 'sdk-original-products.json')
    product_hashes = {}
    with zipfile.ZipFile(archive) as bundle:
        with bundle.open('native-lifetime-evidence/consumer-cli') as stream:
            cli_digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        records.same(cli_digest, original['consumer_cli_sha256'])
        for filename in ('native-sdk-build.json', 'libotelc_native_runtime.so'):
            fact = products['/home/runner/work/_temp/native-lifetime-sdk/' + filename]
            info = bundle.getinfo('native-lifetime-sdk/' + filename)
            assert info.file_size == fact['size']
            with bundle.open(info) as stream:
                product_hashes[filename] = hashlib.file_digest(stream, 'sha256').hexdigest()
            records.same(product_hashes[filename], fact['sha256'])
        audit = HistoricalAudit(root, bundle, original, name)
        functions = []
        for path in reports:
            relative = path.relative_to(root)
            assert relative.parts[0] == name
            arguments = driver.lifetime_cases.expected(language, mode)['arguments']
            if relative.parts[:2] == (name, 'shrinking') and relative.parts[2] != 'original':
                arguments = (0,)
            contracts, function = driver.BASE_CALLS, 'selected'
            if path in promoted:
                contracts = promoted[path]
                function = records.read(path)['cases'][0]['function'].removesuffix('(int)')
                functions.append(function)
            driver.check_report(path, difference, audit, expected=arguments,
                                function=function, contracts=contracts, mode=mode)
        assert set(functions) == {'selected', 'selected_extra'}
        assert set(audit.seen) == set(original['audit']['records'])
        assert len(audit.contexts) == expected_launches // 2
        assert audit.observations == expected_launches + 4 * difference
        assert audit.copied == 4 * difference
        folder, entry, witness, function = next(iter(audit.contexts.values()))
        fresh = refusals.run(folder, entry, witness, out / 'fresh-refusals',
                             function=function, mode=mode)
        records.same(fresh['count'], 14)
        profile = language + '-' + mode
        records.same(original['raw_evidence_refusals'][profile]['refused'], fresh['refused'])
    for path, fact in held.items():
        records.same(records.identity(Path(path)), fact)
    result = {
        'status': 'pass for original captured bytes', 'case': name, 'mode': mode,
        'head': HEAD, 'run': index['run'], 'successful_job': index['successful_job'],
        'archive_sha256': index['archive_sha256'], 'source_catalogues': catalogues,
        'consumer_cli_sha256': cli_digest, 'sdk_original_product_hashes': product_hashes,
        'actual_application_launch_records': len(audit.seen),
        'instrumented_raw_receipts': len(audit.contexts), 'reports': len(reports),
        'current_source_promotion_reports': len(promoted),
        'copied_observations_not_new_launches': audit.copied,
        'fresh_altered_copy_refusals': fresh['count'], 'raw_totals': dict(audit.totals),
        'application_image_hashes': audit.images,
        'selected_evidence_files_preserved': len(held),
        'new_application_launches': 0, 'compiler_builds': 0, 'sdk_builds': 0,
        'limits': 'One named diagnostic fixture only. Captured source, CLI, SDK, image/build, typed outcome, launch-record and raw transport bytes are joined. Historical runner kernel/stat identities and compiler execution are not recertified on this host. No other shard, startup/live, shipping or whole-project admission.',
    }
    (out / 'review.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items()
                      if key != 'application_image_hashes'}, indent=2))


if __name__ == '__main__':
    assert len(sys.argv) == 2
    main(sys.argv[1])
