"""Native site-hook initialisation; no application entry-point execution here."""

from __future__ import annotations

import atexit
import os
import site
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from otelc.config import Policy
from otelc import python_sdk_io as io
from otelc.python_control import ControlServer
from otelc.python_monitor import PythonMonitor
from otelc.python_runtime import PythonRuntime
from otelc.python_sdk_process import SdkChannel, policy_digest


@dataclass
class PythonApplication:
    """Own and close startup resources once, including the atexit registration."""

    runtime: PythonRuntime
    monitor: PythonMonitor
    server: ControlServer
    _closed: bool = field(default=False, init=False)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            atexit.unregister(self.close)
            self.server.close()
            self.monitor.close()
            self.runtime.close()


def chain_sitecustomize(bootstrap: ModuleType) -> None:
    """Let CPython itself load and handle the original site customisation."""
    del sys.modules['sitecustomize']
    try:
        site.execsitecustomize()
    finally:
        # importlib must find its outer, still-loading module even when the
        # application has no sitecustomize or its normal import failed.
        sys.modules.setdefault('sitecustomize', bootstrap)


def initialise(plan_fd: int) -> PythonApplication:
    """Install probes before native Python executes the application's code."""
    plan = io.inherited_plan(plan_fd)
    if set(plan) != {'schema_version', 'policy', 'root', 'directory',
                    'events_fd', 'replies_fd', 'sdk_pid', 'launch_id', 'policy_sha256'}:
        raise ValueError('invalid Python frontend plan')
    if type(plan['schema_version']) is not int or plan['schema_version'] != 1:
        raise ValueError('unsupported Python frontend plan')
    policy = Policy.parse(plan['policy'])
    if policy_digest(policy) != plan['policy_sha256']:
        raise ValueError('Python frontend policy identity differs')
    channel = SdkChannel(plan['events_fd'], plan['replies_fd'], sdk_pid=plan['sdk_pid'],
                         launch_id=plan['launch_id'], policy_sha256=plan['policy_sha256'])
    runtime = monitor = None
    try:
        channel.bind(os.getpid())
        runtime = PythonRuntime(policy, channel)
        monitor = PythonMonitor(policy, Path(plan['root']), runtime)
        if bootstrap := sys.modules.get('sitecustomize'):
            monitor.retire_startup_module(bootstrap)
        monitor.install()
        server = ControlServer(runtime, Path(plan['directory']))
    except BaseException:
        if monitor is not None:
            monitor.close()
        if runtime is not None:
            runtime.reject('bootstrap')
            runtime.close()
        channel.close()
        raise

    application = PythonApplication(runtime, monitor, server)
    atexit.register(application.close)
    return application
