"""Frozen source/compiler bindings for the private synchronous witness profile."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from types import CodeType

from .witness_callsite import bind_site
from .witness_code import CodeBindingError, CodeRegistry, fingerprint
from .witness_state import Bindings, Probe, WitnessState
from .witness_wire import _pairs, _number


MAX_FILES = 256
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_DIRECTORIES = 512
ROLES = ('envelope', 'rendezvous', 'wrapper', 'target')
EDGES = {'envelope': ('primary', 'envelope'), 'rendezvous': ('envelope', 'rendezvous'),
         'wrapper': ('envelope', 'wrapper'), 'target': ('wrapper', 'target')}


def canonical(value: object) -> bytes:
    """Encode producer-owned JSON data deterministically, rejecting nonfinite data."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def selection_id(plan: dict) -> str:
    """Bind admission policy without binding ephemeral signal endpoints/resources."""
    selected = {key: value for key, value in plan.items()
                if key not in ('resource', 'export', 'metrics_endpoint', 'trace_export')}
    selected['runtime'] = {key: value for key, value in plan['runtime'].items() if key != 'control_socket'}
    selected['export_max_queued_batches'] = plan['export']['max_queued_batches']
    return digest(selected)


def inventory(root: Path) -> dict[str, bytes]:
    """Read a bounded source tree; never follow symlinks or open special files."""
    files = {}
    total = 0
    for index, (folder, directories, names) in enumerate(os.walk(root, followlinks=False, onerror=_unreadable_directory)):
        if index >= MAX_DIRECTORIES or len(directories) > MAX_DIRECTORIES:
            raise CodeBindingError('source_inventory_capacity')
        for name in directories:
            if (Path(folder) / name).is_symlink():
                raise CodeBindingError('symlink_source_directory')
        directories[:] = sorted(name for name in directories if name != '__pycache__')
        for name in sorted(names):
            if not name.endswith('.py'):
                continue
            path = Path(folder) / name
            if len(files) >= MAX_FILES:
                raise CodeBindingError('source_inventory_capacity')
            data = read_source(path, MAX_SOURCE_BYTES - total)
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise CodeBindingError('source_inventory_capacity')
            relative = path.relative_to(root).as_posix()
            if len(relative.encode()) > 1024:
                raise CodeBindingError('source_path_capacity')
            files[relative] = data
    return dict(sorted(files.items()))


def _unreadable_directory(error: OSError) -> None:
    raise CodeBindingError('unreadable_source_directory') from error


def read_source(path: Path, remaining: int) -> bytes:
    """Bound the actual read, including a file replaced/grown after enumeration."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise CodeBindingError('unsupported_source_file') from error
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise CodeBindingError('unsupported_source_file')
        if metadata.st_size > remaining:
            raise CodeBindingError('source_inventory_capacity')
        data = source.read(remaining + 1)
    if len(data) > remaining:
        raise CodeBindingError('source_inventory_capacity')
    return data


def adapter_id() -> str:
    """Bind maintained Python adapter sources, independently of application imports."""
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.rglob('*.py')) + [root / 'requirements.txt']
    return digest({path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in paths})


def selected(plan: dict, root: Path, name: str, code: CodeType) -> bool:
    """Apply the same source/function/annotation decisions as the real monitor."""
    from .monitor import annotations, function_identity
    from .policy import Selection
    if not Selection(plan['source_matchers']).accepts(name):
        return False
    tag = annotations(root / name).get(code.co_firstlineno) if plan['annotations']['read_existing'] else None
    return tag != 'otelc.exclude' and Selection(plan['function_matchers']).accepts(
        function_identity(name, code), tag == 'otelc.instrument')


class FrozenProgram:
    """Retain immutable manifest bytes; fresh compiler objects are never bindings.

    This first profile uses ordinary top-level synchronous functions and a fixed
    source root. It freezes Python source, not non-code data or external packages.
    Actual imported code still needs the loader observation in WitnessBootstrap.
    """

    def __init__(self, root: Path, record: dict):
        self.root = root
        self._bytes = canonical(record)
        if len(self._bytes) > MAX_MANIFEST_BYTES:
            raise CodeBindingError('manifest_capacity')

    @classmethod
    def freeze(cls, root: Path, plan: dict, primary: str, roles: dict, offsets: dict):
        if (plan.get('schema_version') != 2 or plan.get('language') != 'python'
                or plan.get('execution_available') is not True or not plan.get('traces', {}).get('enabled')
                or plan.get('lifetimes', {}).get('enabled') or any(plan.get('propagation', {}).values())):
            raise CodeBindingError('unsupported_witness_policy')
        root = root.resolve(strict=True)
        if (not root.is_dir() or type(primary) is not str or type(roles) is not dict
                or set(roles) != set(ROLES) or type(offsets) is not dict or set(offsets) != set(EDGES)):
            raise CodeBindingError('invalid_manifest_declaration')
        files = inventory(root)
        if primary not in files:
            raise CodeBindingError('missing_primary_source')
        compiled = {name: compile(data, str(root / name), 'exec', dont_inherit=True)
                    for name, data in files.items()}
        codes = {'primary': compiled[primary]}
        declarations = {'primary': {'path': primary, 'function': '<module>'}}
        for role in ROLES:
            value = roles[role]
            if (type(value) is not dict or set(value) != {'path', 'function'}
                    or type(value['path']) is not str or value['path'] not in files
                    or type(value['function']) is not str or not value['function'].isidentifier()
                    or role != 'target' and value['path'] != primary):
                raise CodeBindingError('invalid_manifest_role')
            matches = [code for code in compiled[value['path']].co_consts
                       if type(code) is CodeType and code.co_qualname == value['function']]
            if len(matches) != 1:
                raise CodeBindingError('ambiguous_manifest_role')
            codes[role] = matches[0]
            declarations[role] = dict(value)
        if any(not selected(plan, root, declarations[role]['path'], codes[role]) for role in ('wrapper', 'target')):
            raise CodeBindingError('witness_role_not_selected')
        registry = CodeRegistry()
        for code in codes.values():
            registry.register(code)
        sites = {edge: bind_site(registry, codes[caller], codes[callee], offsets[edge]).record()
                 for edge, (caller, callee) in EDGES.items()}
        # OwnerProfile checks the complete connected chain and distinct roles.
        from .witness_owner import OwnerProfile
        OwnerProfile(registry, codes['primary'], *(bind_site(registry, codes[caller], codes[callee], offsets[edge])
                                                   for edge, (caller, callee) in EDGES.items()))
        record = {'version': 1, 'language': 'python', 'profile': 'python-sync-v1',
                  'root': str(root), 'compiler': {'implementation': sys.implementation.name,
                    'version': list(sys.version_info[:3]), 'cache_tag': sys.implementation.cache_tag,
                    'optimization': sys.flags.optimize},
                  'adapter_id': adapter_id(), 'selection_id': selection_id(plan),
                  'sources': {name: {'sha256': hashlib.sha256(data).hexdigest(),
                                    'module_probe_id': fingerprint(compiled[name])}
                              for name, data in files.items()},
                  'roles': {role: {**declarations[role], 'probe_id': fingerprint(code)}
                            for role, code in codes.items()}, 'sites': sites}
        record['build_id'] = digest(record)
        return cls(root, record)

    @classmethod
    def load(cls, data: bytes, root: Path, plan: dict):
        if type(data) is not bytes or len(data) > MAX_MANIFEST_BYTES:
            raise CodeBindingError('manifest_capacity')
        try:
            value = json.loads(data, object_pairs_hook=_pairs, parse_float=_number,
                               parse_constant=_number)
            roles = value['roles']
            primary = roles['primary']['path']
            selectors = {role: {key: roles[role][key] for key in ('path', 'function')} for role in ROLES}
            offsets = {edge: value['sites'][edge]['call_offset'] for edge in EDGES}
            expected = cls.freeze(root, plan, primary, selectors, offsets)
            if canonical(value) != expected.bytes():
                raise CodeBindingError('manifest_mismatch')
            return expected
        except (KeyError, TypeError, ValueError, RecursionError, UnicodeError) as error:
            raise CodeBindingError('invalid_or_changed_manifest') from error

    def bytes(self) -> bytes:
        return self._bytes

    def record(self) -> dict:
        """Return a copy; a caller cannot mutate the retained frozen declaration."""
        return json.loads(self._bytes)

    def state(self) -> WitnessState:
        """Declare expected digests while keeping capability unavailable until bind."""
        record = self.record()
        roles = record['roles']
        return WitnessState(Bindings(record['build_id'], record['selection_id'],
                                     *(Probe(roles[role]['probe_id']) for role in ('rendezvous', 'wrapper', 'target')),
                                     object()))

    def validate_sources(self) -> None:
        actual = {name: hashlib.sha256(data).hexdigest() for name, data in inventory(self.root).items()}
        expected = {name: item['sha256'] for name, item in self.record()['sources'].items()}
        if actual != expected:
            raise CodeBindingError('source_inventory_changed')
