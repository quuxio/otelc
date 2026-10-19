"""Publish generated traffic data without writing to the protected code branch."""
import json
from pathlib import Path
import subprocess
import tempfile


BRANCH = "traffic-data"
REMOTE = "https://github.com/quuxio/otelc.git"
README = "# Generated traffic data\n\nRepository traffic for the README badge, maintained by the Traffic Badge workflow.\n"


def badge(traffic: dict[str, int]) -> str:
    """Reject malformed API counts before generating the Shields endpoint."""
    count, uniques = traffic["count"], traffic["uniques"]
    if type(count) is not int or type(uniques) is not int or not 0 <= uniques <= count:
        raise ValueError("invalid traffic counts")
    result = {"schemaVersion": 1, "label": "traffic",
              "message": f"{uniques:,} unique / {count:,} views / 14d", "color": "blue"}
    return json.dumps(result, separators=(",", ":")) + "\n"


def git(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Use the verified workflow token through gh; never persist it in git config."""
    return subprocess.run(["git", "-C", str(directory), "-c", "credential.helper=",
                           "-c", "credential.helper=!gh auth git-credential", *args],
                          check=check, capture_output=True, text=True)


def publish(directory: Path, remote: str, content: str) -> bool:
    """Create or advance only the data branch; unchanged data makes no commit."""
    if any(directory.iterdir()):
        raise ValueError("traffic workspace must be empty")
    git(directory, "init", "--initial-branch=" + BRANCH)
    git(directory, "remote", "add", "origin", remote)
    git(directory, "config", "user.name", "quuxio")
    git(directory, "config", "user.email", "quuxio@users.noreply.github.com")
    found = git(directory, "ls-remote", "--exit-code", "--heads", "origin", BRANCH, check=False)
    if found.returncode == 0:
        git(directory, "fetch", "--depth=1", "origin", "refs/heads/" + BRANCH)
        entries = git(directory, "ls-tree", "-rz", "FETCH_HEAD").stdout.split("\0")
        for entry in filter(None, entries):
            metadata, name = entry.split("\t", 1)
            if not metadata.startswith("100644 blob ") or name not in {"README.md", ".badges/traffic.json"}:
                raise ValueError("traffic branch contains unexpected files or modes")
        git(directory, "checkout", "-B", BRANCH, "FETCH_HEAD")
    elif found.returncode != 2:
        found.check_returncode()
    target = directory / ".badges/traffic.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(content)
    (directory / "README.md").write_text(README)
    git(directory, "add", "--", ".badges/traffic.json", "README.md")
    changed = git(directory, "diff", "--cached", "--quiet", check=False)
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        changed.check_returncode()
    git(directory, "commit", "-m", "chore: update traffic badge")
    git(directory, "push", "origin", "HEAD:refs/heads/" + BRANCH)
    return True


def main() -> int:
    """Fail before publishing if the secret token belongs to any other identity."""
    try:
        login = subprocess.check_output(["gh", "api", "user", "--jq", ".login"], text=True, stderr=subprocess.PIPE).strip()
        if login != "quuxio":
            raise ValueError("traffic token must belong to quuxio")
        traffic = subprocess.check_output(["gh", "api", "repos/quuxio/otelc/traffic/views"], text=True, stderr=subprocess.PIPE)
        content = badge(json.loads(traffic))
        with tempfile.TemporaryDirectory(prefix="otelc-traffic-") as directory:
            changed = publish(Path(directory), REMOTE, content)
        print("Traffic badge published" if changed else "Traffic badge unchanged")
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print("Traffic badge update failed; verify quuxio identity, traffic access and data-branch write permission.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
