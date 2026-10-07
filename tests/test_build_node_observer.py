"""Qualify exact-version addon builds and reject incompatible Node headers."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from ci import build_node_observer as build


class NodeBuildTests(unittest.TestCase):
    def test_matched_headers_and_coverage_on_mac_and_linux(self):
        info = {"version": "24.11.0", "abi": "137", "executable": "/runtime/bin/node"}
        header = "#define NODE_MAJOR_VERSION 24\n#define NODE_MINOR_VERSION 11\n#define NODE_PATCH_VERSION 0\n#define NODE_MODULE_VERSION 137\n"
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), patch.dict(build.os.environ, {"OTELC_NODE": "/node", "CXX": "compiler", "OTELC_PLUGIN_DIR": "/output", "CARGO_LLVM_COV": "1"}, clear=True), patch.object(build.sys, "platform", platform), patch.object(build.subprocess, "check_output", return_value=json.dumps(info)), patch.object(build.Path, "read_text", return_value=header), patch.object(build.Path, "mkdir"), patch.object(build.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                build.main()
            command = run.call_args.args[0]
            self.assertEqual(command[0], "compiler")
            self.assertIn("-I/runtime/include/node", command)
            self.assertIn("/output/otelc_node_observer.node", command)
            self.assertIn("-fcoverage-mapping", command)
            self.assertEqual("dynamic_lookup" in command, platform == "darwin")
            self.assertTrue(run.call_args.kwargs["check"])

    def test_missing_node_or_mismatched_version_fails_before_compilation(self):
        with patch.dict(build.os.environ, {}, clear=True), patch.object(build.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Node 24"):
                build.main()
        info = {"version": "26.10.0", "abi": "147", "executable": "/runtime/bin/node"}
        header = "#define NODE_MAJOR_VERSION 24\n#define NODE_MINOR_VERSION 11\n#define NODE_PATCH_VERSION 0\n#define NODE_MODULE_VERSION 137\n"
        with patch.dict(build.os.environ, {"OTELC_NODE": "/node"}, clear=True), patch.object(build.subprocess, "check_output", return_value=json.dumps(info)), patch.object(build.Path, "read_text", return_value=header), patch.object(build.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "match the running"):
                build.main()
        run.assert_not_called()
