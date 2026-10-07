"""Build the public-V8 observer against the exact running Node headers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def main():
    environment = os.environ.copy()
    node = environment.get("OTELC_NODE") or shutil.which("node")
    if not node:
        raise ValueError("Node 24.11+ is required")
    info = json.loads(subprocess.check_output([node, "-p", "JSON.stringify({version:process.versions.node,abi:process.versions.modules,executable:process.execPath})"], text=True))
    if tuple(map(int, info["version"].split("."))) < (24, 11, 0):
        raise ValueError("Node 24.11+ is required")
    include = Path(environment.get("OTELC_NODE_INCLUDE", str(Path(info["executable"]).resolve().parent.parent / "include/node")))
    header = (include / "node_version.h").read_text()
    versions = [re.search(r"#define NODE_" + part + r"_VERSION\s+(\d+)", header).group(1) for part in ("MAJOR", "MINOR", "PATCH")]
    abi = re.search(r"#define NODE_MODULE_VERSION\s+(\d+)", header).group(1)
    if ".".join(versions) != info["version"] or abi != info["abi"]:
        raise ValueError("Node headers must match the running version and module ABI")
    compiler = environment.get("CXX") or environment.get("CC") or ("/opt/homebrew/opt/llvm@22/bin/clang++" if sys.platform == "darwin" else "clang++")
    output = Path(environment.get("OTELC_PLUGIN_DIR", "target/debug"))
    output.mkdir(parents=True, exist_ok=True)
    library = output / "otelc_node_observer.node"
    command = [compiler, "-std=c++20", "-shared", "-fPIC", "-O2", "-I" + str(include), "native/node/PromiseObserver.cpp", "-o", str(library)]
    if sys.platform == "darwin":
        command.extend(["-undefined", "dynamic_lookup"])
    if environment.get("CARGO_LLVM_COV"):
        command.extend(["-fprofile-instr-generate", "-fcoverage-mapping"])
    subprocess.run(command, check=True, env=environment)
    print(f"Promise observer: Node {info['version']} ABI {abi}; {library}")


if __name__ == "__main__":
    main()
