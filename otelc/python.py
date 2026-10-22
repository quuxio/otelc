"""Candidate native Python launcher: original sources plus generated startup hook."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from otelc.config import Policy
from otelc import python_sdk_io as io
from otelc.python_sdk_process import SdkProcess

ROOT = Path(__file__).resolve().parent.parent


def invocation(root: Path, *, entry: Path | None, module: str | None,
               arguments: tuple[str, ...]) -> list[str]:
    """Validate the declared script/module profile before starting any SDK."""
    if sys.implementation.name != 'cpython' or sys.version_info < (3, 12):
        raise ValueError('Python frontend requires CPython 3.12+')
    if (entry is None) == (module is None):
        raise ValueError('choose exactly one Python script or module')
    if entry is not None:
        path = entry if entry.is_absolute() else root / entry
        actual = path.resolve(strict=True)
        if not actual.is_file() or actual.suffix != '.py' or not actual.is_relative_to(root):
            raise ValueError('Python entry must be a source .py file inside the application root')
        # Root is the command cwd. Preserve the caller's spelling and native
        # __file__/argv semantics rather than substituting a generated runner.
        return [sys.executable, str(entry), *arguments]
    if type(module) is not str or not all(part.isidentifier() for part in module.split('.')):
        raise ValueError('invalid Python module entry')
    return [sys.executable, '-m', module, *arguments]


def startup_text(hook: Path, directory: Path, plan_fd: int,
                 original_pythonpath: str | None) -> str:
    """Restore import search state before app startup hooks and original code."""
    return f'''import os, sys
_bootstrap = sys.modules[__name__]
sys.path.remove({str(hook)!r})
_original_pythonpath = {original_pythonpath!r}
if _original_pythonpath is None:
    os.environ.pop('PYTHONPATH', None)
else:
    os.environ['PYTHONPATH'] = _original_pythonpath
sys.path.insert(0, {str(ROOT)!r})
try:
    from otelc.python_startup import initialise, chain_sitecustomize
    sys.path.remove({str(ROOT)!r})
    initialise({plan_fd})
except BaseException:
    # CPython normally swallows site-hook failures. Instrumentation startup
    # must fail before app execution, without formatting application objects.
    try:
        from otelc import python_sdk_io as _io
        from pathlib import Path as _Path
        _fd = _io.create_file(_Path({str(directory)!r}) / 'python-bootstrap-error.json')
        try:
            _io.write_json(_fd, {{'schema_version': 1, 'phase': 'bootstrap_failed'}})
        finally:
            os.close(_fd)
    finally:
        os._exit(78)
chain_sitecustomize(_bootstrap)
'''


def prepare(policy: Policy, directory: Path, sdk: SdkProcess, root: Path
            ) -> tuple[int, dict[str, str]]:
    """Generate only tooling-owned files; never rewrite an application file."""
    hook = directory / 'bootstrap'
    hook.mkdir(mode=0o700)
    events, replies = sdk.channel.descriptors
    descriptor = os.open(directory / 'python-plan.json',
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        io.write_json(descriptor, {'schema_version': 1, 'policy': policy.data,
            'root': str(root), 'directory': str(directory),
            'events_fd': events, 'replies_fd': replies, 'sdk_pid': sdk.process.pid,
            'launch_id': sdk.channel.launch_id, 'policy_sha256': sdk.channel.policy_sha256})
        os.lseek(descriptor, 0, os.SEEK_SET)
        original = os.environ.get('PYTHONPATH')
        generated = io.create_file(hook / 'sitecustomize.py')
        try:
            with os.fdopen(generated, 'w', closefd=False) as output:
                output.write(startup_text(hook, directory, descriptor, original))
        finally:
            os.close(generated)
        environment = {**os.environ, 'PYTHONPATH': os.pathsep.join(
            [str(hook)] + ([original] if original is not None else []))}
        return descriptor, environment
    except BaseException:
        os.close(descriptor)
        raise


def run(policy: Policy, directory: Path, *, root: Path, entry: Path | None = None,
        module: str | None = None, arguments: tuple[str, ...] = (),
        plain: bool = False) -> int:
    """Wait for SDK drain outside the app, preserving a failing app's exit code."""
    root = root.resolve(strict=True)
    command = invocation(root, entry=entry, module=module, arguments=arguments)
    if plain:
        return subprocess.call(command, cwd=root)
    validate_policy(policy)
    directory = directory.resolve()
    directory.mkdir(mode=0o700)
    sdk = SdkProcess.start(policy, directory / 'sdk', bind=False)
    descriptor = None
    application = None
    try:
        descriptor, environment = prepare(policy, directory, sdk, root)
        application = subprocess.Popen(command, cwd=root, env=environment,
            pass_fds=(*sdk.channel.descriptors, descriptor))
        sdk.channel.close()
        os.close(descriptor)
        descriptor = None
        result = application.wait()
        try:
            report = sdk.wait_report(application_pid=application.pid)
            clean = report['phase'] == 'closed'
        except (ValueError, OSError, subprocess.SubprocessError):
            clean = False
        if not clean:
            print('Python SDK drain failed or remains unverified', file=sys.stderr)
        return result if result else 0 if clean else 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if application is not None and application.poll() is None:
            application.terminate()
            try:
                application.wait(timeout=1)
            except subprocess.TimeoutExpired:
                application.kill()
                application.wait(timeout=2)
        sdk.abort()


def validate_policy(policy: Policy) -> None:
    """The CLI and benchmark must reject the same unimplemented profile."""
    if 'python' not in policy.data['languages']:
        raise ValueError('Python is disabled by policy')
    if policy.data['context']['http']:
        raise ValueError('Python candidate HTTP-context integration is not available yet')
