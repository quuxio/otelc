"""Check or enable otelc's Dependabot settings using only the saved quuxio login."""
import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

REPOSITORY = "quuxio/otelc"
PREFIX = f"repos/{REPOSITORY}"


class SecurityError(RuntimeError):
    """A check cannot establish the requested live security state."""


@dataclass(frozen=True)
class Response:
    status: int
    data: object = None
    next_page: str | None = None


def next_page(header):
    link = re.search(r"(?im)^link:\s*(.+)$", header)
    match = re.search(r'<([^>]+)>;\s*rel="next"', link[1]) if link else None
    if not match:
        if link and 'rel="next"' in link[1]:
            raise SecurityError("GitHub returned an invalid next-page link; no complete alert count available")
        return None
    target = urlsplit(match[1])
    if target.scheme != "https" or target.netloc != "api.github.com" or target.path != f"/{PREFIX}/dependabot/alerts" or target.fragment:
        raise SecurityError("Refusing a Dependabot pagination link outside this repository")
    return target.path.lstrip("/") + "?" + target.query


class GitHub:
    def __init__(self):
        self.executable = "/opt/homebrew/bin/gh" if Path("/opt/homebrew/bin/gh").is_file() else shutil.which("gh")
        if not self.executable:
            raise SecurityError("Install the GitHub CLI and authenticate quuxio first")
        allowed = ("HOME", "PATH", "TMPDIR", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "GH_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
        self.environment = {key: os.environ[key] for key in allowed if key in os.environ}
        self.environment["GH_HOST"] = "github.com"
        self.environment["GH_PROMPT_DISABLED"] = "1"
        try:
            result = subprocess.run([self.executable, "auth", "token", "--hostname", "github.com", "--user", "quuxio"], env=self.environment.copy(), capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SecurityError("Cannot obtain the saved quuxio credential") from error
        if result.returncode or not result.stdout.strip():
            raise SecurityError("No saved quuxio credential; authenticate quuxio with the GitHub CLI")
        self.token = result.stdout.strip()
        self.environment["GH_TOKEN"] = self.token

    def request(self, method, endpoint):
        if method not in ("GET", "PUT") or (method == "PUT" and endpoint not in (PREFIX + "/vulnerability-alerts", PREFIX + "/automated-security-fixes")):
            raise SecurityError("Refusing an unsupported GitHub security operation")
        # Keep credentials on github.com and operations on this fixed repository.
        if endpoint != "user" and not (endpoint == PREFIX or endpoint.startswith(PREFIX + "/")):
            raise SecurityError("Refusing a GitHub operation outside quuxio/otelc")
        try:
            result = subprocess.run([self.executable, "api", "--hostname", "github.com", "--include", "--method", method, endpoint], env=self.environment, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SecurityError("GitHub request did not complete within its deadline") from error
        header, separator, body = result.stdout.partition("\n\n")
        status = re.match(r"HTTP/\S+ (\d{3})\b", header)
        if not separator or not status:
            raise SecurityError("GitHub did not return a valid HTTP response")
        try:
            data = json.loads(body) if body.strip() else None
        except json.JSONDecodeError as error:
            raise SecurityError("GitHub returned invalid JSON") from error
        if result.returncode and int(status[1]) < 400:
            raise SecurityError("GitHub CLI failed despite an HTTP success response")
        return Response(int(status[1]), data, next_page(header))

    def expect(self, method, endpoint, expected=200):
        return self.validate(method, endpoint, self.request(method, endpoint), expected)

    def validate(self, method, endpoint, response, expected=200):
        status, data = response.status, response.data
        if status != expected:
            message = data.get("message", "request rejected") if isinstance(data, dict) else "request rejected"
            message = str(message).replace(self.token, "[redacted]")
            raise SecurityError(f"GitHub {method} {endpoint} returned HTTP {status}: {message}")
        return data


def identity_and_repository(client):
    actor = client.expect("GET", "user")
    if not isinstance(actor, dict) or actor.get("login") != "quuxio":
        raise SecurityError("Refusing GitHub operations: authenticated identity is not quuxio")
    repository = client.expect("GET", PREFIX)
    permissions = repository.get("permissions") if isinstance(repository, dict) else None
    if not isinstance(repository, dict) or repository.get("full_name") != REPOSITORY or not isinstance(permissions, dict) or permissions.get("admin") is not True:
        raise SecurityError("quuxio must have repository administration permission")


def settings(client):
    status = client.request("GET", PREFIX + "/vulnerability-alerts").status
    if status not in (204, 404):
        raise SecurityError(f"Cannot verify Dependabot alerts: GitHub returned HTTP {status}")
    updates = client.expect("GET", PREFIX + "/automated-security-fixes")
    if not isinstance(updates, dict) or not isinstance(updates.get("enabled"), bool) or not isinstance(updates.get("paused"), bool):
        raise SecurityError("GitHub returned invalid security-update settings")
    return {"alerts_enabled": status == 204, "security_updates_enabled": updates["enabled"], "security_updates_paused": updates["paused"]}


def alert_severity(value, numbers):
    if not isinstance(value, dict) or value.get("state") != "open":
        raise SecurityError("GitHub returned an invalid open alert")
    number = value.get("number")
    if type(number) is not int or number <= 0 or number in numbers:
        raise SecurityError("GitHub returned an invalid or repeated alert number")
    advisory = value.get("security_advisory")
    severity = advisory.get("severity") if isinstance(advisory, dict) else None
    if severity not in ("low", "moderate", "high", "critical"):
        raise SecurityError("GitHub returned an alert without a recognised severity")
    numbers.add(number)
    return severity


def alerts(client):
    severities = Counter()
    endpoint = f"{PREFIX}/dependabot/alerts?state=open&per_page=100"
    seen = set()
    numbers = set()
    for _ in range(100):
        if endpoint in seen:
            raise SecurityError("GitHub repeated a Dependabot pagination cursor")
        seen.add(endpoint)
        response = client.request("GET", endpoint)
        values = client.validate("GET", endpoint, response)
        if not isinstance(values, list):
            raise SecurityError("GitHub returned invalid Dependabot alerts")
        for value in values:
            severities[alert_severity(value, numbers)] += 1
        if response.next_page is None:
            return dict(sorted(severities.items()))
        endpoint = response.next_page
    raise SecurityError("Dependabot pagination exceeds 100 pages; no complete alert count available")


def run(client, enable=False):
    identity_and_repository(client)
    current = settings(client)
    if enable:
        if not current["alerts_enabled"]:
            client.expect("PUT", PREFIX + "/vulnerability-alerts", 204)
        if not current["security_updates_enabled"]:
            client.expect("PUT", PREFIX + "/automated-security-fixes", 204)
        current = settings(client)
    if not current["alerts_enabled"] or not current["security_updates_enabled"] or current["security_updates_paused"]:
        raise SecurityError("Dependabot alerts/security updates are disabled or paused; use --enable for disabled settings and inspect GitHub settings for paused updates")
    counts = alerts(client)
    return {"repository": REPOSITORY, "actor": "quuxio", **current, "open_alerts": sum(counts.values()), "severity_counts": counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable", action="store_true", help="Enable alerts and security updates, then verify the live state")
    args = parser.parse_args()
    try:
        result = run(GitHub(), args.enable)
    except SecurityError as error:
        parser.exit(1, f"otelc security: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
