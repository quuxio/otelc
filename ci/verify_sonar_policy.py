"""Verify the explicit SonarQube Cloud new-code policy before analysis."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

PROJECT_KEY = "quuxio_otelc"
POLICY_KEY = "sonar.leak.period"
EXPECTED_POLICY = "previous_version"
POLICY_URL = (
    "https://sonarcloud.io/api/settings/values"
    f"?component={PROJECT_KEY}&keys={POLICY_KEY}"
)


def validate_policy(payload: dict) -> None:
    """Require an explicit project value, not an inherited baseline."""
    settings = payload.get("settings", [])
    if not isinstance(settings, list):
        raise ValueError("SonarQube returned an invalid settings response")
    matching = [item for item in settings if isinstance(item, dict) and item.get("key") == POLICY_KEY]
    if (
        len(matching) != 1
        or matching[0].get("value") != EXPECTED_POLICY
        or matching[0].get("inherited", False) is not False
    ):
        raise ValueError("Set the project's new-code definition to Previous version")


def verify_remote(token: str) -> None:
    """Read only the fixed project endpoint without logging credentials."""
    authorization = base64.b64encode(f"{token}:".encode()).decode()
    request = urllib.request.Request(
        POLICY_URL,
        headers={"Authorization": f"Basic {authorization}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("SonarQube returned an invalid settings response")
    validate_policy(payload)


def main() -> int:
    """Fail closed when the credential, endpoint, or project policy is invalid."""
    token = os.environ.get("SONAR_TOKEN", "")
    if not token:
        print("SONAR_TOKEN is required for the policy check", file=sys.stderr)
        return 1
    try:
        verify_remote(token)
    except (urllib.error.URLError, TimeoutError, ValueError):
        print("SonarQube policy verification failed; check access and Previous version settings", file=sys.stderr)
        return 1
    print(f"Verified {PROJECT_KEY}: project-level Previous version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
