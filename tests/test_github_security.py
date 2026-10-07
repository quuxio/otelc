"""Exercise security setup without writing to GitHub or exposing credentials."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ci import github_security as security


class Client:
    def __init__(self, alerts=True, updates=True, paused=False):
        self.enabled = alerts
        self.updates = updates
        self.paused = paused
        self.actor = "quuxio"
        self.admin = True
        self.requests = []
        self.pages = [[]]

    def request(self, method, endpoint):
        self.requests.append((method, endpoint))
        if method == "PUT":
            if endpoint.endswith("vulnerability-alerts"):
                self.enabled = True
            else:
                self.updates = True
            return security.Response(204)
        if endpoint == "user":
            return security.Response(200, {"login": self.actor})
        if endpoint == security.PREFIX:
            return security.Response(200, {"full_name": security.REPOSITORY, "permissions": {"admin": self.admin}})
        if endpoint.endswith("vulnerability-alerts"):
            return security.Response(204 if self.enabled else 404)
        if endpoint.endswith("automated-security-fixes"):
            return security.Response(200, {"enabled": self.updates, "paused": self.paused})
        index = int(parse_qs(urlsplit(endpoint).query).get("after", [0])[0])
        next_page = f"{security.PREFIX}/dependabot/alerts?state=open&per_page=100&after={index + 1}" if index + 1 < len(self.pages) else None
        return security.Response(200, self.pages[index], next_page)

    expect = security.GitHub.expect
    validate = security.GitHub.validate
    token = "test-secret"


class SecurityTests(unittest.TestCase):
    def test_disabled_settings_fail_check_then_enable_idempotently(self):
        client = Client(False, False)
        with self.assertRaisesRegex(security.SecurityError, "disabled or paused"):
            security.run(client)
        self.assertFalse(any(method == "PUT" for method, _ in client.requests))
        result = security.run(client, True)
        self.assertEqual(result["open_alerts"], 0)
        self.assertTrue(result["alerts_enabled"])
        self.assertTrue(result["security_updates_enabled"])
        writes = [item for item in client.requests if item[0] == "PUT"]
        self.assertEqual(writes, [("PUT", security.PREFIX + "/vulnerability-alerts"), ("PUT", security.PREFIX + "/automated-security-fixes")])
        security.run(client, True)
        self.assertEqual(writes, [item for item in client.requests if item[0] == "PUT"])

    def test_wrong_identity_admin_permission_and_paused_updates_fail(self):
        client = Client(False, False)
        client.actor = "stephenlclarke"
        with self.assertRaisesRegex(security.SecurityError, "not quuxio"):
            security.run(client, True)
        self.assertEqual(client.requests, [("GET", "user")])
        client.actor, client.admin = "quuxio", False
        with self.assertRaisesRegex(security.SecurityError, "administration"):
            security.run(client, True)
        self.assertFalse(any(method == "PUT" for method, _ in client.requests))
        with self.assertRaisesRegex(security.SecurityError, "paused"):
            security.run(Client(paused=True), True)

    def test_alert_pagination_counts_all_severities_without_advisory_contents(self):
        client = Client()
        def alert(number, severity):
            return {"number": number, "state": "open", "security_advisory": {"severity": severity, "summary": "private details"}}
        client.pages = [[alert(number, "high") for number in range(1, 101)], [alert(101, "critical")]]
        result = security.run(client)
        self.assertEqual(result["open_alerts"], 101)
        self.assertEqual(result["severity_counts"], {"critical": 1, "high": 100})
        self.assertNotIn("private details", json.dumps(result))
        client.pages = [[alert(1, "high")], [alert(2, "critical")]]
        self.assertEqual(security.run(client)["open_alerts"], 2)
        for invalid in ({}, [{"state": "closed"}], [{"number": 1, "state": "open", "security_advisory": {}}], [alert(1, "high"), alert(1, "high")]):
            client.pages = [invalid]
            with self.assertRaises(security.SecurityError):
                security.run(client)

    def test_pagination_uses_verified_links_and_rejects_cycles_and_incomplete_counts(self):
        target = f"https://api.github.com/{security.PREFIX}/dependabot/alerts?state=open&per_page=100&after=cursor"
        self.assertEqual(security.next_page(f'Link: <{target}>; rel="next"'), target.removeprefix("https://api.github.com/"))
        self.assertIsNone(security.next_page(f'Link: <{target}>; rel="prev"'))
        with self.assertRaisesRegex(security.SecurityError, "no complete alert count"):
            security.next_page(f'Link: {target}; rel="next"')
        for bad in (target.replace("https:", "http:"), target.replace("api.github.com", "elsewhere.example"), target.replace("quuxio/otelc", "elsewhere/project"), target + "#fragment"):
            with self.assertRaisesRegex(security.SecurityError, "outside"):
                security.next_page(f'Link: <{bad}>; rel="next"')
        client = Client()
        original = client.request
        client.request = lambda method, endpoint: security.Response(200, [], endpoint) if "/dependabot/alerts?" in endpoint else original(method, endpoint)
        with self.assertRaisesRegex(security.SecurityError, "repeated"):
            security.run(client)
        client = Client()
        client.pages = [[]] * 101
        with self.assertRaisesRegex(security.SecurityError, "no complete alert count"):
            security.run(client)

    def test_missing_cli_and_empty_saved_token_are_errors(self):
        with patch.object(security.Path, "is_file", return_value=False), patch.object(security.shutil, "which", return_value=None):
            with self.assertRaisesRegex(security.SecurityError, "Install"):
                security.GitHub()
        with patch.object(security.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, " \n", "")):
            with self.assertRaisesRegex(security.SecurityError, "No saved quuxio"):
                security.GitHub()

    def test_errors_cannot_be_misreported_as_disabled_or_zero_alerts(self):
        client = Client(False, False)
        original = client.request
        client.request = lambda method, endpoint: security.Response(403, {"message": "scope denied"}) if endpoint.endswith("vulnerability-alerts") else original(method, endpoint)
        with self.assertRaisesRegex(security.SecurityError, "HTTP 403"):
            security.run(client, True)
        self.assertFalse(any(method == "PUT" for method, _ in client.requests))
        client = Client()
        client.request = lambda method, endpoint: security.Response(403, {"message": "test-secret denied"})
        with self.assertRaisesRegex(security.SecurityError, r"HTTP 403: \[redacted\] denied"):
            security.run(client)
        original = Client().request
        with patch.object(client, "request", side_effect=lambda method, endpoint: security.Response(200, {"enabled": True}) if endpoint.endswith("automated-security-fixes") else original(method, endpoint)):
            with self.assertRaisesRegex(security.SecurityError, "invalid security-update"):
                security.settings(client)

    def test_cli_uses_saved_quuxio_token_ignores_inherited_tokens_and_checks_host(self):
        token = subprocess.CompletedProcess([], 0, "test-secret\n", "")
        response = subprocess.CompletedProcess([], 0, 'HTTP/2.0 200 OK\nContent-Type: application/json\n\n{"login":"quuxio"}', "")
        with patch.dict(security.os.environ, {"PATH": "/usr/bin", "HOME": "/test", "GH_TOKEN": "other-account", "GITHUB_TOKEN": "other-account", "GH_HOST": "other-host", "GH_DEBUG": "api", "UNRELATED_SECRET": "do-not-inherit"}, clear=True), patch.object(security.subprocess, "run", side_effect=[token, response]) as command:
            client = security.GitHub()
            self.assertEqual(client.expect("GET", "user"), {"login": "quuxio"})
        auth, api = command.call_args_list
        self.assertIn("quuxio", auth.args[0])
        self.assertFalse("GH_TOKEN" in auth.kwargs["env"], "auth lookup must ignore inherited tokens")
        self.assertEqual(api.kwargs["env"]["GH_TOKEN"], "test-secret")
        self.assertEqual(api.kwargs["env"]["GH_HOST"], "github.com")
        self.assertFalse("GH_DEBUG" in api.kwargs["env"], "API debugging must remain disabled")
        self.assertFalse("UNRELATED_SECRET" in api.kwargs["env"], "unrelated credentials must not be inherited")
        with self.assertRaisesRegex(security.SecurityError, "outside"):
            client.request("GET", "repos/elsewhere/project")
        with self.assertRaisesRegex(security.SecurityError, "unsupported"):
            client.request("DELETE", security.PREFIX)

    def test_cli_failures_are_redacted_and_do_not_print_a_success_report(self):
        token = subprocess.CompletedProcess([], 0, "test-secret\n", "")
        for failure in (subprocess.CompletedProcess([], 1, "", "test-secret"), OSError("test-secret"), subprocess.TimeoutExpired("test-secret", 30)):
            with patch.object(security.subprocess, "run", side_effect=[failure]):
                with self.assertRaises(security.SecurityError) as error:
                    security.GitHub()
                self.assertNotIn("test-secret", str(error.exception))
        with patch.object(security.subprocess, "run", return_value=token):
            client = security.GitHub()
        for response in (subprocess.CompletedProcess([], 1, "invalid", "test-secret"), subprocess.CompletedProcess([], 1, "HTTP/2.0 200 OK\n\n{}", "test-secret"), subprocess.CompletedProcess([], 0, "HTTP/2.0 200 OK\n\nnot json", ""), OSError("test-secret"), subprocess.TimeoutExpired("test-secret", 30)):
            with patch.object(security.subprocess, "run", side_effect=[response]):
                with self.assertRaises(security.SecurityError) as error:
                    client.request("GET", "user")
                self.assertNotIn("test-secret", str(error.exception))
        with patch.object(security, "GitHub", side_effect=security.SecurityError("denied")), patch.object(security, "__name__", "test"), patch.object(security.os, "environ", {}), patch("sys.argv", ["security"]), contextlib.redirect_stderr(io.StringIO()) as stderr, contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as error:
                security.main()
        self.assertEqual(error.exception.code, 1)
        self.assertIn("denied", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        with patch.object(security, "GitHub", return_value=Client()), patch("sys.argv", ["security", "--enable"]), contextlib.redirect_stdout(io.StringIO()) as stdout:
            security.main()
        self.assertEqual(json.loads(stdout.getvalue())["actor"], "quuxio")
