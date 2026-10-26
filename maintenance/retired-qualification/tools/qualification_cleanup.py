"""Retire explicitly admitted stale caches and verified expanded records."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import qualification_record as record

REGISTRY = "qualification-retention.json"
DAYS = {"cache": 7, "expanded": 14}
CACHE_NAMES = {"build-cache", "compiler-cache", "incremental", "CMakeFiles",
               "__pycache__", "npm-cache", "full-go-cache"}


def protected_paths(root: Path, registry: dict) -> list[str]:
    """Protect archives backing any admitted expanded record."""
    pins = list(registry["pins"])
    for entry in registry["candidates"].values():
        if entry.get("archive"):
            archive = Path(entry["archive"])
            if archive.is_relative_to(root):
                pins.append(archive.relative_to(root).as_posix())
    return pins


def in_use(path: Path) -> bool:
    """Refuse uncertain process inspection rather than delete live inputs."""
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-t", "+D", str(path)],
                            capture_output=True, text=True, timeout=30,
                            check=False)
    if result.returncode not in (0, 1) or result.stderr:
        raise ValueError("Open-file inspection was incomplete")
    return bool(result.stdout.strip())


def target(root: Path, relative: str, pins: list[str]) -> Path:
    """Constrain retirement to unpinned nested directories in this workspace."""
    if not record.valid_name(relative) or len(Path(relative).parts) < 2:
        raise ValueError("Target must be a nested workspace directory")
    path = root / relative
    for name in pins:
        if not record.valid_name(name):
            raise ValueError("Invalid pin")
        pin = root / name
        if path.is_relative_to(pin) or pin.is_relative_to(path):
            raise ValueError("Target overlaps a retention pin")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError("Symlink target or ancestor")
    if not path.is_dir():
        raise ValueError("Target is not an existing directory")
    return path


def fingerprint(path: Path) -> tuple[str, int]:
    """Bind file identities and modification times without reading caches."""
    inventory = record.snapshot(path)
    facts = {name: record.identity(info) for name, info in inventory.items()}
    encoded = json.dumps(facts, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest(), max(
        info.st_mtime_ns for info in inventory.values())


def archive_matches(path: Path, archive: Path) -> str:
    """Require a verified byte-for-byte archive of this exact expanded root."""
    manifest = record.verify(archive)
    inventory = record.snapshot(path)
    if manifest["original_root"] != str(path) or set(inventory) != set(
            manifest["entries"]):
        raise ValueError("Archive root or member set differs")
    for name, info in inventory.items():
        expected = manifest["entries"][name]
        actual = record.metadata(info)
        if any(actual[key] != expected[key] for key in
               ("mode", "mtime_ns", "directory")):
            raise ValueError("Archive metadata differs")
        if not expected["directory"] and (
                actual["size"] != expected["size"] or
                record.digest(path / name) != expected["sha256"]):
            raise ValueError("Archive content differs")
    return record.digest(archive)


def save(root: Path, registry: dict) -> None:
    """Publish the small operational registry atomically under its lock."""
    fd, name = tempfile.mkstemp(prefix=".retention-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(registry, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, root / REGISTRY)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def register(root: Path, registry: dict, relative: str, kind: str,
             reason: str, confirmed_unused: bool,
             archive: Path | None = None) -> None:
    """Admit data only after an explicit terminal-state/reference review."""
    if not confirmed_unused or not reason.strip():
        raise ValueError("Confirm unused state and supply a review reason")
    if kind not in DAYS:
        raise ValueError("Unsupported retirement class")
    path = target(root, relative, protected_paths(root, registry))
    if kind == "cache" and not (path.name in CACHE_NAMES or (
            path.name == "build" and path.parent.name == "go-cache")):
        raise ValueError("Mixed build/source directories are not cache targets")
    if relative in registry["candidates"]:
        raise ValueError("Already registered; review changed data separately")
    signature, modified_ns = fingerprint(path)
    entry = {"kind": kind, "reason": reason, "registered_ns": time.time_ns(),
             "last_modified_ns": modified_ns, "fingerprint": signature}
    if kind == "expanded":
        if archive is None:
            raise ValueError("Expanded records require a verified archive")
        archive = archive.absolute()
        if archive.is_relative_to(path):
            raise ValueError("Archive must survive retirement of its source")
        if any(archive.is_relative_to(root / name)
               for name in registry["candidates"]):
            raise ValueError("Archive is inside another retirement candidate")
        entry.update(archive=str(archive),
                     archive_sha256=archive_matches(path, archive))
    registry["candidates"][relative] = entry


def clean(root: Path, registry: dict, apply: bool = False) -> list[dict]:
    """Report eligible data; delete only unchanged, unpinned admitted targets."""
    results = []
    for relative, entry in list(registry["candidates"].items()):
        result = {"path": relative, "status": "blocked"}
        try:
            path = target(root, relative, protected_paths(root, registry))
            signature, modified_ns = fingerprint(path)
            if signature != entry["fingerprint"]:
                raise ValueError("Target changed since admission")
            eligible_ns = max(entry["registered_ns"], modified_ns) + (
                DAYS[entry["kind"]] * 86400 * 1_000_000_000)
            if time.time_ns() < eligible_ns:
                result.update(status="waiting", eligible_ns=eligible_ns)
            else:
                if entry["kind"] == "expanded" and archive_matches(
                        path, Path(entry["archive"])) != entry["archive_sha256"]:
                    raise ValueError("Retained archive changed since admission")
                result["status"] = "eligible"
                if apply:
                    if in_use(path):
                        raise ValueError("Target has open files or a live user")
                    shutil.rmtree(path)
                    del registry["candidates"][relative]
                    result["status"] = "removed"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            result.update(status="blocked", reason=str(error))
        results.append(result)
    return results


def main() -> int:
    """Serialise registration and cleanup; dry-run unless --apply is supplied."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="action", required=True)
    admission = sub.add_parser("register")
    admission.add_argument("path")
    admission.add_argument("--kind", choices=DAYS, required=True)
    admission.add_argument("--reason", required=True)
    admission.add_argument("--confirmed-unused", action="store_true")
    admission.add_argument("--archive", type=Path)
    cleanup = sub.add_parser("clean")
    cleanup.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    with (root / ".qualification-retention.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry = json.loads((root / REGISTRY).read_text())
        if registry["schema_version"] != 1:
            raise ValueError("Unsupported retention registry")
        if args.action == "register":
            register(root, registry, args.path, args.kind, args.reason,
                     args.confirmed_unused, args.archive)
            save(root, registry)
        else:
            before = shutil.disk_usage(root).free
            results = clean(root, registry, args.apply)
            if args.apply:
                save(root, registry)
            print(json.dumps({"apply": args.apply, "targets": results,
                              "free_bytes_before": before,
                              "free_bytes_after": shutil.disk_usage(root).free}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
