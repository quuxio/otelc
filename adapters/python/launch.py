"""External launch adapter: quux-otelc python SCRIPT or python -m MODULE."""
import json
import inspect
import os
from importlib.machinery import SourceFileLoader, SourcelessFileLoader
from pkgutil import get_importer
from types import ModuleType
import runpy
import sys
from pathlib import Path

from quux_otelc_python.monitor import Monitor, function_identity
from quux_otelc_python.telemetry import Runtime



def run_script(argument: str) -> None:
    """Use ordinary script metadata while retaining the caller's argv spelling."""
    filename = argument if os.path.isabs(argument) else os.path.join(os.getcwd(), argument)
    # Keep the existing directory/zip entry-point support.
    if get_importer(filename) is not None:
        runpy.run_path(argument, run_name="__main__")
        return
    bytecode = filename.endswith(".pyc")
    loader = (SourcelessFileLoader if bytecode else SourceFileLoader)("__main__", filename)
    code = loader.get_code("__main__") if bytecode else compile(
        Path(filename).read_bytes(), filename, "exec", dont_inherit=True)
    module = ModuleType("__main__")
    module.__dict__.update(__file__=filename, __cached__=None,
                           __package__=None, __spec__=None, __loader__=loader)
    previous = sys.modules["__main__"]
    sys.modules["__main__"] = module
    try:
        exec(code, module.__dict__)
    finally:
        sys.modules["__main__"] = previous


def main(args: list[str]) -> int:
    if len(args) < 2:
        raise ValueError("python requires SCRIPT [ARGS...] or -m MODULE [ARGS...]")
    plan = json.loads(Path(args[0]).read_text())
    if plan["language"] != "python" or not plan["execution_available"]:
        raise ValueError("Python adapter requires an executable Python policy")
    if not hasattr(sys, "monitoring"):
        raise ValueError("Python instrumentation requires CPython 3.12+")
    if args[1] == "--doctor":
        signals = "metrics"
        if plan.get("traces", {}).get("enabled", False):
            from quux_otelc_python import traces  # Check requested SDK support before launch.
            signals += " and function spans"
            if plan.get("propagation", {}).get("tasks", False):
                signals += " with standard asyncio task context and ThreadPoolExecutor context"
            if plan.get("lifetimes", {}).get("enabled", False):
                signals += " and ordinary own-initializer weak-referenceable class collection lifetimes"
        print(f"Python {sys.version.split()[0]}: CPython monitoring and OTLP/HTTP {signals} available")
        return 0
    if args[1] == "--inspect":
        if len(args) < 3 or any(a != "--json" for a in args[3:]):
            raise ValueError("Python inspect requires SCRIPT [--json]")
        source = Path(args[2]).resolve()
        monitor = Monitor(plan, None)
        code = compile(source.read_bytes(), str(source), "exec")
        functions = []

        def visit(block):
            for value in block.co_consts:
                if hasattr(value, "co_consts"):
                    if value.co_flags & inspect.CO_NEWLOCALS:
                        functions.append({"name": function_identity(source.relative_to(Path.cwd()).as_posix(), value), "selected": monitor.display_name(value) is not None})
                    visit(value)

        visit(code)
        print(json.dumps({"language": "python", "functions": functions}, indent=2))
        return 0
    runtime = Runtime(plan)
    monitor = None
    try:
        monitor = Monitor(plan, runtime)
        monitor.install()
        if args[1] == "-m":
            if len(args) < 3:
                raise ValueError("-m requires a module name")
            sys.argv = args[2:]
            sys.path[0] = str(Path.cwd())
            runpy.run_module(args[2], run_name="__main__", alter_sys=True)
        else:
            sys.argv = args[1:]
            sys.path[0] = str(Path(args[1]).resolve().parent)
            run_script(args[1])
    finally:
        if monitor:
            monitor.close()
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
