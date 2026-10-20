"""Exercise traffic publishing against real local git remotes and identity failures."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ci import update_traffic_badge as traffic


class TrafficBadgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.command("init", "--bare", "--initial-branch=main", str(self.remote))
        seed = self.root / "seed"
        seed.mkdir()
        self.command("-C", str(seed), "init", "--initial-branch=main")
        self.command("-C", str(seed), "config", "user.name", "quuxio")
        self.command("-C", str(seed), "config", "user.email", "quuxio@users.noreply.github.com")
        (seed / "application.py").write_text("print('source unchanged')\n")
        self.command("-C", str(seed), "add", ".")
        self.command("-C", str(seed), "commit", "-m", "test: initialise protected code")
        self.command("-C", str(seed), "push", str(self.remote), "main")
        self.original = self.command("--git-dir", str(self.remote), "rev-parse", "main")

    def command(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()

    def publish(self, name: str, content: str) -> bool:
        directory = self.root / name
        directory.mkdir()
        return traffic.publish(directory, str(self.remote), content)

    def assert_main_unchanged(self):
        self.assertEqual(self.command("--git-dir", str(self.remote), "rev-parse", "main"), self.original)
        self.assertEqual(self.command("--git-dir", str(self.remote), "show", "main:application.py"), "print('source unchanged')")

    def test_new_branch_unchanged_data_and_fast_forward_preserve_main(self):
        content = traffic.badge({"count": 1234, "uniques": 12})
        self.assertTrue(self.publish("first", content))
        self.assertFalse(self.publish("same", content))
        self.assertTrue(self.publish("changed", traffic.badge({"count": 1235, "uniques": 13})))
        self.assertEqual(self.command("--git-dir", str(self.remote), "rev-list", "--count", "traffic-data"), "2")
        paths = self.command("--git-dir", str(self.remote), "ls-tree", "-r", "--name-only", "traffic-data").splitlines()
        self.assertEqual(paths, [".badges/traffic.json", "README.md"])
        author = self.command("--git-dir", str(self.remote), "show", "-s", "--format=%an <%ae>", "traffic-data")
        self.assertEqual(author, "quuxio <quuxio@users.noreply.github.com>")
        self.assert_main_unchanged()

    def test_unexpected_data_branch_content_is_not_overwritten(self):
        self.command("-C", str(self.root / "seed"), "push", str(self.remote), "main:traffic-data")
        content = traffic.badge({"count": 1, "uniques": 1})
        with self.assertRaisesRegex(ValueError, "unexpected"):
            self.publish("unsafe", content)
        self.assertEqual(self.command("--git-dir", str(self.remote), "rev-parse", "traffic-data"), self.original)
        self.assert_main_unchanged()

    def test_concurrent_update_is_rejected_without_force_pushing(self):
        self.publish("initial", traffic.badge({"count": 1, "uniques": 1}))
        original_git = traffic.git
        concurrent = traffic.badge({"count": 3, "uniques": 2})
        inject = True

        def racing(directory, *args, **kwargs):
            nonlocal inject
            if args[0] == "push" and inject:
                inject = False
                self.publish("concurrent", concurrent)
            return original_git(directory, *args, **kwargs)

        stale = traffic.badge({"count": 2, "uniques": 1})
        with patch.object(traffic, "git", side_effect=racing):
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish("stale", stale)
        latest = self.command("--git-dir", str(self.remote), "show", "traffic-data:.badges/traffic.json")
        self.assertEqual(json.loads(latest), json.loads(concurrent))
        self.assert_main_unchanged()

    def test_api_counts_validate_before_rendering(self):
        self.assertEqual(json.loads(traffic.badge({"count": 1234, "uniques": 12}))["message"], "12 unique / 1,234 views / 14d")
        self.assertEqual(json.loads(traffic.badge({"count": 0, "uniques": 0}))["message"], "0 unique / 0 views / 14d")
        for value in ({"count": True, "uniques": 0}, {"count": 1, "uniques": 2}, {"count": -1, "uniques": 0}):
            with self.assertRaises(ValueError):
                traffic.badge(value)

    def test_existing_workspace_and_symlinked_remote_files_are_rejected(self):
        content = traffic.badge({"count": 1, "uniques": 1})
        with self.assertRaisesRegex(ValueError, "empty"):
            traffic.publish(self.root / "seed", str(self.remote), content)
        self.publish("valid", content)
        seed = self.root / "seed"
        self.command("-C", str(seed), "fetch", str(self.remote), "traffic-data")
        self.command("-C", str(seed), "checkout", "-B", "traffic-data", "FETCH_HEAD")
        outside = self.root / "outside.txt"
        outside.write_text("preserve this file")
        (seed / "README.md").unlink()
        (seed / "README.md").symlink_to(outside)
        self.command("-C", str(seed), "add", "README.md")
        self.command("-C", str(seed), "commit", "-m", "test: reject data symlink")
        self.command("-C", str(seed), "push", str(self.remote), "traffic-data")
        with self.assertRaisesRegex(ValueError, "modes"):
            self.publish("unsafe-symlink", content)
        self.assertEqual(outside.read_text(), "preserve this file")
        self.assert_main_unchanged()

    def test_main_verifies_quuxio_and_reports_failures_without_sensitive_values(self):
        for outcome in (False, True):
            with patch.object(traffic.subprocess, "check_output", side_effect=["quuxio\n", '{"count":2,"uniques":1}']), patch.object(traffic, "publish", return_value=outcome) as publish:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(traffic.main(), 0)
                self.assertEqual(publish.call_args.args[1], traffic.REMOTE)
        for value in ("other-identity", "quuxio\n"):
            replies = [value, "private-malformed-traffic"]
            output = io.StringIO()
            with patch.object(traffic.subprocess, "check_output", side_effect=replies), patch.object(traffic, "publish") as publish:
                with contextlib.redirect_stdout(output):
                    self.assertEqual(traffic.main(), 1)
                publish.assert_not_called()
            self.assertNotIn("private-malformed-traffic", output.getvalue())
            self.assertNotIn("other-identity", output.getvalue())
