"""Check matched-toolchain enforcement and plugin metadata without a compiler."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ci import build_llvm_plugin


class PluginBuildTests(unittest.TestCase):
    def test_matched_toolchain_preserves_flags_and_writes_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {"LLVM_CONFIG": "/matched/llvm-config", "OTELC_PLUGIN_DIR": directory, "CARGO_LLVM_COV": "1", "MARKER": "kept"}
            answers = ["22.1.8", "/matched/bin", '-I"/headers with spaces" -std=c++17', "-L/lib -lLLVM"]
            with patch.dict(build_llvm_plugin.os.environ, settings, clear=True), patch.object(build_llvm_plugin.subprocess, "check_output", side_effect=answers), patch.object(build_llvm_plugin.subprocess, "run") as run:
                build_llvm_plugin.main()
            command = run.call_args.args[0]
            self.assertEqual(command[0], "/matched/bin/clang++")
            self.assertIn("-I/headers with spaces", command)
            self.assertIn("-fcoverage-mapping", command)
            self.assertEqual(run.call_args.kwargs["env"]["MARKER"], "kept")
            metadata = json.loads((Path(directory) / "otelc-llvm-toolchain.json").read_text())
            self.assertEqual(metadata["bindir"], "/matched/bin")
            self.assertEqual(metadata["version"], "22.1.8")

    def test_incompatible_llvm_rejected_before_compilation(self):
        with patch.dict(build_llvm_plugin.os.environ, {"LLVM_CONFIG": "wrong"}, clear=True), patch.object(build_llvm_plugin.subprocess, "check_output", return_value="21.1.0"), patch.object(build_llvm_plugin.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "LLVM 22"):
                build_llvm_plugin.main()
        run.assert_not_called()

    def test_homebrew_matching_install_precedes_path(self):
        with patch.dict(build_llvm_plugin.os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=True), patch.object(build_llvm_plugin.subprocess, "check_output", return_value="21.0") as query:
            with self.assertRaises(ValueError):
                build_llvm_plugin.main()
        self.assertEqual(query.call_args.args[0][0], "/opt/homebrew/opt/llvm@22/bin/llvm-config")

    def test_missing_tools_rejected(self):
        with patch.dict(build_llvm_plugin.os.environ, {}, clear=True), patch.object(Path, "is_file", return_value=False), patch.object(build_llvm_plugin.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Install LLVM 22"):
                build_llvm_plugin.main()
