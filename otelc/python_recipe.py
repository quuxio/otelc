"""Fixed native Python argv derived from retained caller intent, never receipts."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from otelc.config import Policy
from otelc.file_inputs import checked_file, identity

# This is a declared input scope, not an unrestricted environment dump. In
# particular, application credentials and GitHub/Sonar credentials stay out.
ENVIRONMENT_KEYS = (
    'PYTHONHOME', 'PYTHONPATH', 'PYTHONPLATLIBDIR', 'PYTHONSAFEPATH',
    'PYTHONNOUSERSITE', 'PYTHONUSERBASE', 'PYTHONUTF8', 'PYTHONCOERCECLOCALE',
    'PYTHONIOENCODING', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE',
    'PYTHONPYCACHEPREFIX', 'PYTHONOPTIMIZE', 'PYTHONVERBOSE', 'PYTHONINSPECT',
    'PYTHONWARNINGS', 'PYTHONHASHSEED', 'PYTHONDEVMODE', 'PYTHONFAULTHANDLER',
    'PYTHONPROFILEIMPORTTIME', 'PYTHONMALLOC', 'PYTHONMALLOCSTATS',
    'PYTHONTRACEMALLOC', 'PYTHONASYNCIODEBUG', 'PYTHONBREAKPOINT',
    'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'TEMP', 'TMP',
)
FIELDS = {'schema_version', 'profile', 'policy', 'policy_source', 'cwd',
          'paths', 'module', 'arguments', 'flags', 'plain', 'interpreter',
          'environment'}
INPUT_PROFILE = 'python-native-inputs-v1'
ORIGIN_PROFILE = 'python-native-origins-v2'
CACHE_PROFILE = 'python-native-cache-origins-v3'


def version(profile: str) -> int:
    """Keep the existing input contract separate from explicit origin evidence."""
    versions = {INPUT_PROFILE: 1, ORIGIN_PROFILE: 2, CACHE_PROFILE: 3}
    if type(profile) is not str or profile not in versions:
        raise ValueError('unsupported native Python profile')
    return versions[profile]


def encoded(value: Any) -> str:
    """Use one byte-exact JSON recipe for generated requests and resolved plans."""
    return json.dumps(value, sort_keys=True, allow_nan=False) + '\n'


def environment_snapshot(environment: dict[str, str]) -> dict[str, str | None]:
    """Preserve absent versus empty values only for the declared input keys."""
    return {key: environment.get(key) for key in ENVIRONMENT_KEYS}


def _text(value: object, *, empty: bool = False) -> bool:
    return type(value) is str and (empty or bool(value)) and '\0' not in value


def _path(path: Path | str, base: Path) -> dict[str, str]:
    selected = Path(path)
    selected = selected if selected.is_absolute() else base / selected
    return {'requested': str(path), 'resolved': str(selected.resolve(strict=True))}


def native_argv0() -> str:
    """Observe the installed launcher's argv0 without app hooks or import paths.

    Homebrew framework launchers can substitute their real framework binary.
    This fixed probe always uses our selected interpreter, never a receipt path.
    """
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('PYTHON', 'OTEL_', 'OTELC_',
                                          'GH_', 'GITHUB_', 'SONAR_'))}
    return json.loads(subprocess.check_output([sys.executable, '-I', '-S', '-c',
        'import json,sys;print(json.dumps(sys.orig_argv[0]))'], text=True, timeout=10,
        cwd=Path(__file__).resolve().parent.parent, env=environment))


def request(policy: Policy, runtime: Path | str, *, root: Path | str, entry: Path | str | None,
            module: str | None, arguments: tuple[str, ...] = (),
            plain: bool = False, profile: str = INPUT_PROFILE) -> dict[str, Any]:
    """Keep the original spelling and full policy separate from resolved settings.

    Runtime must already exist. Entry paths resolve from the requested app root;
    root/runtime paths resolve from the caller cwd. No application is imported.
    """
    cwd = Path.cwd().resolve(strict=True)
    paths = {'root': _path(root, cwd), 'runtime': _path(runtime, cwd)}
    if entry is not None:
        paths['entry'] = _path(entry, Path(paths['root']['resolved']))
    result = {'schema_version': version(profile), 'profile': profile,
              'policy': copy.deepcopy(policy.data),
              'policy_source': copy.deepcopy(policy.source), 'cwd': str(cwd),
              'paths': paths, 'module': module, 'arguments': list(arguments),
              'flags': [], 'plain': plain,
              'interpreter': {'file': identity(Path(sys.executable)),
                              'version': sys.version,
                              'cache_tag': sys.implementation.cache_tag,
                              'argv0': native_argv0()},
              'environment': environment_snapshot(os.environ)}
    resolve_plan(result)
    return result


def _validate(original: dict[str, Any]) -> Policy:
    if type(original) is not dict or set(original) != FIELDS:
        raise ValueError('invalid native Python request fields')
    if type(original['policy']) is not dict:
        raise ValueError('invalid native Python common policy')
    policy = Policy.parse(original['policy'])
    if (type(original['schema_version']) is not int or original['schema_version'] != version(original['profile'])
            or original['policy'] != policy.data or 'python' not in policy.data['languages']
            or type(original['plain']) is not bool or original['flags'] != []
            or type(original['arguments']) is not list
            or any(not _text(item, empty=True) for item in original['arguments'])):
        raise ValueError('invalid native Python request values')
    _validate_environment(original['environment'])
    _validate_interpreter(original['interpreter'])
    _validate_paths(original)
    if version(original['profile']) >= 2 and original['module'] is not None:
        raise ValueError('native Python origins currently require a source entry')
    return policy


def _validate_environment(environment: object) -> None:
    if (type(environment) is not dict or set(environment) != set(ENVIRONMENT_KEYS)
            or any(value is not None and not _text(value, empty=True)
                   for value in environment.values())):
        raise ValueError('invalid native Python environment')


def _validate_interpreter(interpreter: object) -> None:
    if (type(interpreter) is not dict or set(interpreter) != {'file', 'version', 'cache_tag', 'argv0'}
            or not _text(interpreter['version']) or not _text(interpreter['cache_tag'])
            or not _text(interpreter['argv0'])
            or type(interpreter['file']) is not dict
            or not _text(interpreter['file'].get('path'))
            or not Path(interpreter['file']['path']).is_absolute()):
        raise ValueError('invalid native Python interpreter identity')
    file = interpreter['file']
    if (set(file) not in ({'path', 'resolved_path', 'sha256', 'size'},
                         {'path', 'resolved_path', 'sha256', 'size', 'symlink'})
            or not _text(file.get('resolved_path'))
            or not Path(file['resolved_path']).is_absolute()
            or type(file.get('size')) is not int or file['size'] < 0
            or type(file.get('sha256')) is not str or not re.fullmatch('[a-f0-9]{64}', file['sha256'])
            or 'symlink' in file and not _text(file['symlink'])):
        raise ValueError('invalid native Python interpreter file record')


def _validate_paths(original: dict[str, Any]) -> None:
    paths, module = original['paths'], original['module']
    if (type(paths) is not dict or set(paths) not in ({'root', 'runtime'}, {'root', 'runtime', 'entry'})
            or not _text(original['cwd']) or not Path(original['cwd']).is_absolute()
            or ('entry' in paths) == (module is not None)
            or module is not None and (not _text(module) or not all(part.isidentifier() for part in module.split('.')))):
        raise ValueError('invalid native Python entry or paths')
    for item in paths.values():
        if (type(item) is not dict or set(item) != {'requested', 'resolved'}
                or not all(_text(value) for value in item.values())
                or not Path(item['resolved']).is_absolute()):
            raise ValueError('invalid native Python path record')
    if 'entry' in paths:
        entry = paths['entry']
        resolved = Path(entry['resolved'])
        if (resolved.suffix != '.py'
                or not resolved.is_relative_to(Path(paths['root']['resolved']))):
            raise ValueError('native Python entry must be a source .py inside the app root')


def resolve_plan(original: dict[str, Any]) -> dict[str, Any]:
    """Derive common settings and native argv without trusting a recorded command."""
    policy = _validate(original)
    if original['module'] is None:
        spelling = original['paths']['entry']['requested']
        # Preserve caller intent in the request; protect only the concrete
        # interpreter argument, as the public frontend does for the same file.
        target = ['./' + spelling if spelling.startswith('-') else spelling]
    else:
        target = ['-m', original['module']]
    return {'schema_version': version(original['profile']), 'profile': original['profile'],
            'policy': copy.deepcopy(policy.data), 'plain': original['plain'],
            'cwd': original['paths']['root']['resolved'],
            'runtime': original['paths']['runtime']['resolved'],
            'python': original['interpreter']['file']['path'],
            'argv0': original['interpreter']['argv0'],
            'native_arguments': [*target, *original['arguments']],
            'input_scope': {INPUT_PROFILE: 'native-source-entry-only',
                            ORIGIN_PROFILE: 'native-application-root-origins',
                            CACHE_PROFILE: 'native-application-root-source-and-cache-origins'}[original['profile']],
            'source_complete': False}


def verify(original: dict[str, Any], plan: dict[str, Any], *,
           environment: dict[str, str] | None = None) -> None:
    """Recheck original intent and inputs before launch and after actual SDK drain.

    This does not establish imported bytes, code bindings or downstream receipt.
    The supervisor supplies its original environment, before bootstrap injection.
    """
    if resolve_plan(original) != plan:
        raise ValueError('native Python plan differs from the fixed recipe')
    cwd = Path(original['cwd'])
    if cwd.resolve(strict=True) != cwd:
        raise ValueError('native Python caller cwd resolution changed')
    for name, item in original['paths'].items():
        base = Path(original['paths']['root']['resolved']) if name == 'entry' else cwd
        if _path(item['requested'], base) != item:
            raise ValueError('native Python original path resolution changed: ' + name)
        if name != 'entry' and not Path(item['resolved']).is_dir():
            raise ValueError('native Python root/runtime must be directories')
    if 'entry' in original['paths'] and not Path(original['paths']['entry']['resolved']).is_file():
        raise ValueError('native Python entry must be a source file')
    if identity(Path(original['interpreter']['file']['path'])) != original['interpreter']['file']:
        raise ValueError('native Python interpreter file changed')
    if (original['interpreter']['version'] != sys.version
            or original['interpreter']['cache_tag'] != sys.implementation.cache_tag
            or original['interpreter']['argv0'] != native_argv0()):
        raise ValueError('native Python interpreter metadata differs')
    if environment_snapshot(os.environ if environment is None else environment) != original['environment']:
        raise ValueError('native Python original environment changed')
    source = original['policy_source']
    if source is not None:
        if type(source) is not dict or not _text(source.get('path')) or not _text(source.get('text')):
            raise ValueError('invalid native Python policy source')
        metadata, content = checked_file(Path(source['path']), include_content=True)
        if (metadata != {key: value for key, value in source.items() if key != 'text'}
                or content.decode('utf8') != source['text']
                or Policy.parse(tomllib.loads(source['text'])).data != original['policy']):
            raise ValueError('native Python original policy source changed')


def native_command(original: dict[str, Any], driver: Path, journal_fd: int) -> list[str]:
    """Use the verified native tool, original interpreter spelling and inherited FD.

    The owner must verify the driver's native build manifest separately.
    The caller supplies original intent independently of execution receipts;
    no recorded argv or command is accepted as authority for this derivation.
    """
    plan = resolve_plan(original)
    if type(journal_fd) is not int or not 0 <= journal_fd <= 2 ** 31 - 1:
        raise ValueError('invalid native Python journal descriptor')
    if not driver.is_absolute():
        raise ValueError('native Python driver must be an independently verified absolute path')
    option = {ORIGIN_PROFILE: '--origin-loader-root', CACHE_PROFILE: '--origin-loader-caches-root'}.get(original['profile'])
    options = [option, plan['cwd']] if option is not None else []
    return [str(driver), *options, str(journal_fd), plan['python'], plan['argv0'],
            *plan['native_arguments']]
