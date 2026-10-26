"""Physical directory boundaries for generated instrumentation artefacts."""
from __future__ import annotations

from pathlib import Path


def within(path: Path, root: Path) -> bool:
    """Include existing ancestor identities when resolved spelling has case aliases."""
    path = path.resolve()
    root = root.resolve(strict=True)
    if path.is_relative_to(root):
        return True
    original = root.stat()
    for parent in (path, *path.parents):
        try:
            current = parent.stat()
        except FileNotFoundError:
            continue
        if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
            return True
    return False
