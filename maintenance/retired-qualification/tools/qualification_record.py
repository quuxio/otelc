"""Write, verify and restore compressed local qualification records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile

MANIFEST = ".qualification/manifest.json"
CHUNK = 1024 * 1024


def digest(path: Path) -> str:
    """Hash a file without loading it into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(root: Path) -> dict[str, os.stat_result]:
    """Inventory regular files and directories; refuse links and special files."""
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("Evidence root must be a regular directory")
    result = {"": root_info}
    def unreadable(error: OSError) -> None:
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False,
                                          onerror=unreadable):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            result[path.relative_to(root).as_posix()] = path.lstat()
    for name, info in result.items():
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(f"Not a regular file or directory: {name}")
    return result


def identity(info: os.stat_result) -> tuple[int, ...]:
    """Exclude access time, which reading the source may change."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def metadata(info: os.stat_result) -> dict:
    return {"mode": stat.S_IMODE(info.st_mode), "mtime_ns": info.st_mtime_ns,
            "directory": stat.S_ISDIR(info.st_mode), "size": info.st_size}


def valid_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and "\\" not in name and (
        all(part not in ("", ".", "..") for part in name.split("/")))


def payload(archive: Path) -> dict:
    """Verify every decoded payload, its size and the exact member set."""
    with zipfile.ZipFile(archive) as packed:
        names = packed.namelist()
        manifest = json.loads(packed.read(MANIFEST))
        if manifest["format"] != 1:
            raise ValueError("Unsupported qualification format")
        records = manifest["entries"]
        expected = {MANIFEST}
        for name, item in records.items():
            if name and not valid_name(name):
                raise ValueError("Invalid archive path")
            if name == "" and not item["directory"]:
                raise ValueError("Invalid archive root")
            if item["directory"]:
                continue
            expected.add("data/" + name)
            hasher = hashlib.sha256()
            size = 0
            with packed.open("data/" + name) as stream:
                while chunk := stream.read(CHUNK):
                    hasher.update(chunk)
                    size += len(chunk)
            if size != item["size"] or hasher.hexdigest() != item["sha256"]:
                raise ValueError(f"Payload mismatch: {name}")
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError("Unexpected or duplicate archive members")
        if "" not in records:
            raise ValueError("Missing root metadata")
        return manifest


def receipt_path(archive: Path) -> Path:
    return archive.with_name(archive.name + ".json")


def verify(archive: Path) -> dict:
    """Check the external archive digest and all compressed records."""
    receipt = json.loads(receipt_path(archive).read_text())
    if digest(archive) != receipt["archive_sha256"]:
        raise ValueError("Archive hash mismatch")
    manifest = payload(archive)
    if len(manifest["entries"]) != receipt["entries"]:
        raise ValueError("Receipt count mismatch")
    return manifest


def pack(source: Path, archive: Path) -> dict:
    """Compress a terminal record and publish its verified receipt."""
    source = source.absolute()
    archive = archive.absolute()
    if archive.is_relative_to(source):
        raise ValueError("Archive must be outside its source")
    if receipt_path(archive).exists():
        raise FileExistsError(receipt_path(archive))
    before = snapshot(source)
    records = {name: metadata(info) for name, info in before.items()}
    with archive.open("xb") as output:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as packed:
            for name, info in before.items():
                if stat.S_ISDIR(info.st_mode):
                    continue
                path = source / name
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    if identity(os.fstat(stream.fileno())) != identity(info):
                        raise ValueError(f"Source changed: {name}")
                    hasher = hashlib.sha256()
                    with packed.open("data/" + name, "w", force_zip64=True) as sink:
                        while chunk := stream.read(CHUNK):
                            hasher.update(chunk)
                            sink.write(chunk)
                    records[name]["sha256"] = hasher.hexdigest()
            manifest = {"format": 1, "original_root": str(source),
                        "entries": records}
            packed.writestr(MANIFEST, json.dumps(manifest, sort_keys=True))
        output.flush()
        os.fsync(output.fileno())
    after = snapshot(source)
    if {k: identity(v) for k, v in before.items()} != {
            k: identity(v) for k, v in after.items()}:
        raise ValueError("Source changed during compression")
    payload(archive)
    receipt = {"format": 1, "archive": archive.name,
               "archive_sha256": digest(archive), "entries": len(records),
               "original_bytes": sum(v["size"] for v in records.values()
                                     if not v["directory"]),
               "compressed_bytes": archive.stat().st_size}
    with receipt_path(archive).open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    verify(archive)
    return receipt


def restore(archive: Path, destination: Path) -> None:
    """Restore bytes, modes and nanosecond mtimes into a new directory."""
    manifest = verify(archive)
    destination.mkdir(mode=0o700)
    records = manifest["entries"]
    with zipfile.ZipFile(archive) as packed:
        for name, item in sorted(records.items(), key=lambda pair: (
                pair[0].count("/"), pair[0])):
            path = destination / name
            if name and item["directory"]:
                path.mkdir(mode=0o700)
            elif not item["directory"]:
                with packed.open("data/" + name) as source, path.open("xb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
                if digest(path) != item["sha256"]:
                    raise ValueError(f"Restored file mismatch: {name}")
        # Apply directory metadata last so child writes cannot change it.
        for name, item in sorted(records.items(), reverse=True):
            path = destination / name
            os.chmod(path, item["mode"])
            os.utime(path, ns=(item["mtime_ns"], item["mtime_ns"]))


def run(archive: Path, command: list[str]) -> int:
    """Capture a command and retain only compressed completed evidence."""
    if not command:
        raise ValueError("A qualification command is required")
    if archive.exists() or receipt_path(archive).exists():
        raise FileExistsError(archive)
    stage = Path(tempfile.mkdtemp(prefix=".qualification-",
                                  dir=archive.absolute().parent))
    print(f"Active evidence: {stage}", flush=True)
    env = os.environ.copy()
    env["OTELC_QUALIFICATION_DIR"] = str(stage)
    started = time.time_ns()
    with (stage / "stdout.log").open("xb") as out, (
            stage / "stderr.log").open("xb") as err:
        try:
            result = subprocess.run(command, env=env, stdout=out, stderr=err,
                                    check=False)
            code = result.returncode
        except OSError as error:
            err.write(str(error).encode())
            code = 127
    with (stage / "command.json").open("x") as stream:
        json.dump({"argv": command, "cwd": os.getcwd(), "started_ns": started,
                   "finished_ns": time.time_ns(), "returncode": code}, stream,
                  indent=2)
        stream.write("\n")
    pack(stage, archive)
    shutil.rmtree(stage)
    return code if code >= 0 else 128 - code


def main() -> int:
    """Provide the common qualification workflow entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    runner = sub.add_parser("run", help="Run and compress success or failure")
    runner.add_argument("archive", type=Path)
    runner.add_argument("command", nargs=argparse.REMAINDER)
    packer = sub.add_parser("pack", help="Compress existing evidence; keep originals")
    packer.add_argument("source", type=Path)
    packer.add_argument("archive", type=Path)
    checker = sub.add_parser("verify", help="Verify archive and every payload")
    checker.add_argument("archive", type=Path)
    restorer = sub.add_parser("restore", help="Restore into a new directory")
    restorer.add_argument("archive", type=Path)
    restorer.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.action == "run":
        command = args.command
        return run(args.archive, command[1:] if command[:1] == ["--"] else command)
    if args.action == "pack":
        print(json.dumps(pack(args.source, args.archive)))
    elif args.action == "verify":
        manifest = verify(args.archive)
        print(f"Verified {len(manifest['entries'])} entries")
    else:
        restore(args.archive, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
