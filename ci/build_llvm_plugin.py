"""Build the exception-aware pass against the installed, matched LLVM toolchain."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys


def main():
    environment = os.environ.copy()
    homebrew = Path("/opt/homebrew/opt/llvm@22/bin/llvm-config")
    config = environment.get("LLVM_CONFIG") or (str(homebrew) if homebrew.is_file() else shutil.which("llvm-config"))
    if not config:
        raise ValueError("Install LLVM 22 or set LLVM_CONFIG")
    def query(*arguments):
        return subprocess.check_output([config, *arguments], text=True).strip()
    version = query("--version")
    if version.split(".")[0] != "22":
        raise ValueError("The local exception backend requires LLVM 22")
    directory = Path(query("--bindir"))
    output = Path(environment.get("OTELC_PLUGIN_DIR", "target/debug"))
    output.mkdir(parents=True, exist_ok=True)
    plugin = output / "libotelc_pass.dylib" if sys.platform == "darwin" else output / "libotelc_pass.so"
    command = [str(directory / "clang++"), "-shared", "-fPIC", "-O2", *shlex.split(query("--cxxflags")), "native/llvm/OtelcPass.cpp", *shlex.split(query("--ldflags", "--libs", "core", "passes", "--system-libs")), "-o", str(plugin)]
    if environment.get("CARGO_LLVM_COV"):
        command.extend(["-fprofile-instr-generate", "-fcoverage-mapping"])
    subprocess.run(command, check=True, env=environment)
    (output / "otelc-llvm-toolchain.json").write_text(json.dumps({"version": version, "bindir": str(directory), "plugin": plugin.name}) + "\n")
    print(f"Exception backend: LLVM {version}; {plugin}")


if __name__ == "__main__":
    main()
