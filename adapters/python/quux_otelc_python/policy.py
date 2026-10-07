"""Use the resolver's byte-oriented glob expressions without reinterpreting globs."""
import re
from pathlib import Path


class Selection:
    def __init__(self, matchers: dict):
        self.include = [re.compile(p.removeprefix("(?-u)").encode(), re.DOTALL) for p in matchers["include"]]
        self.exclude = [re.compile(p.removeprefix("(?-u)").encode(), re.DOTALL) for p in matchers["exclude"]]

    def accepts(self, name: str, annotated: bool = False) -> bool:
        value = name.encode()
        return (annotated or any(p.fullmatch(value) for p in self.include)) and not any(
            p.fullmatch(value) for p in self.exclude
        )


def source_name(filename: str, root: Path) -> str | None:
    try:
        path = Path(filename).resolve().relative_to(root)
    except (ValueError, OSError):
        return None
    if any(part in (".venv", "node_modules", "__pycache__", "site-packages") for part in path.parts):
        return None
    return path.as_posix()
