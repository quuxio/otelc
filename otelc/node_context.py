"""Node package-file context shared by source and preload input records."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from otelc.file_inputs import identity


def package_context(application: Path) -> list[dict[str, Any]]:
    """Bind the nearest package and every nearer absence that can affect format."""
    records = []
    directory = application.parent
    while True:
        filename = directory / 'package.json'
        if os.path.lexists(filename):
            records.append(identity(filename))
            return records
        records.append({'path': str(filename), 'absent': True})
        if directory.parent == directory:
            return records
        directory = directory.parent
