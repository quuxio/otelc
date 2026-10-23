"""Isolated CLI host: import installed SDK packages without running site hooks."""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sdk_paths() -> tuple[str, ...]:
    """Derive explicit installation paths; never process .pth or app PYTHONPATH.

    CPython3.12 no-site startup has not yet applied venv prefixes. Preserve the
    selected executable's venv spelling instead of resolving its symlink first.
    """
    prefix = Path(sys.executable).parent.parent
    if (prefix / 'pyvenv.cfg').is_file():
        # Under -S, Homebrew 3.12 selects its framework scheme, whose package
        # paths ignore base/platbase. Select the virtualenv scheme explicitly.
        paths = sysconfig.get_paths(scheme='venv', vars={
            'base': str(prefix), 'platbase': str(prefix)})
    else:
        paths = sysconfig.get_paths()
    return tuple(dict.fromkeys(paths[key] for key in ('purelib', 'platlib')))


def main() -> int:
    """Use the ordinary dispatcher only after controlled interpreter startup."""
    if not sys.flags.isolated or not sys.flags.no_site:
        print('quux-otelc: use bin/quux-otelc for isolated CLI startup', file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT))
    install_sdk_paths()
    from otelc.__main__ import main as dispatch
    return dispatch()


def install_sdk_paths() -> None:
    """Expose installed SDK wheels while leaving startup hooks unexecuted."""
    for path in sdk_paths():
        if path not in sys.path:
            sys.path.append(path)


if __name__ == '__main__':
    raise SystemExit(main())
