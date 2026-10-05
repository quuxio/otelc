"""Exercise policy enforcement and credential-safe failure paths offline."""

import contextlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from ci import verify_sonar_policy as policy


def response_for(payload: object) -> io.BytesIO:
    """Provide a context-managed HTTP response with deterministic JSON."""
    return io.BytesIO(json.dumps(payload).encode())


class PolicyTests(unittest.TestCase):
    def test_explicit_previous_version_is_accepted(self) -> None:
        policy.validate_policy({"settings": [{"key": policy.POLICY_KEY, "value": "previous_version"}]})

    def test_missing_inherited_rolling_or_duplicate_policy_is_rejected(self) -> None:
        invalid = [
            {},
            {"settings": "invalid"},
            {"settings": ["invalid"]},
            {"settings": [{"key": policy.POLICY_KEY, "parentValue": "previous_version"}]},
            {"settings": [{"key": policy.POLICY_KEY, "value": "30"}]},
            {"settings": [{"key": policy.POLICY_KEY, "value": "previous_version"}] * 2},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                policy.validate_policy(payload)

    def test_remote_request_uses_fixed_project_and_timeout(self) -> None:
        payload = {"settings": [{"key": policy.POLICY_KEY, "value": "previous_version"}]}
        with patch.object(policy.urllib.request, "urlopen", return_value=response_for(payload)) as opened:
            policy.verify_remote("test-token")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, policy.POLICY_URL)
        self.assertEqual(opened.call_args.kwargs["timeout"], 15)
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_invalid_remote_payload_is_rejected(self) -> None:
        with patch.object(policy.urllib.request, "urlopen", return_value=response_for([])):
            with self.assertRaises(ValueError):
                policy.verify_remote("test-token")

    def test_main_without_token_fails_without_network_access(self) -> None:
        output = io.StringIO()
        with patch.dict(policy.os.environ, {}, clear=True), contextlib.redirect_stderr(output):
            with patch.object(policy, "verify_remote") as remote:
                self.assertEqual(policy.main(), 1)
                remote.assert_not_called()
        self.assertIn("SONAR_TOKEN is required", output.getvalue())

    def test_main_network_or_policy_failure_does_not_disclose_token(self) -> None:
        failures = [urllib.error.URLError("test-token"), TimeoutError("test-token"), ValueError("test-token")]
        for failure in failures:
            output = io.StringIO()
            with self.subTest(failure=failure), patch.dict(policy.os.environ, {"SONAR_TOKEN": "test-token"}):
                with patch.object(policy, "verify_remote", side_effect=failure), contextlib.redirect_stderr(output):
                    self.assertEqual(policy.main(), 1)
            self.assertNotIn("test-token", output.getvalue())

    def test_main_valid_policy_succeeds(self) -> None:
        output = io.StringIO()
        with patch.dict(policy.os.environ, {"SONAR_TOKEN": "test-token"}):
            with patch.object(policy, "verify_remote") as remote, contextlib.redirect_stdout(output):
                self.assertEqual(policy.main(), 0)
                remote.assert_called_once_with("test-token")
        self.assertIn(policy.PROJECT_KEY, output.getvalue())
        self.assertNotIn("test-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
