"""External launch adapter: quux-otelc python SCRIPT or python -m MODULE."""
import atexit
import builtins
import json
import importlib.util
import inspect
import os
from importlib.machinery import ModuleSpec, PathFinder, SourceFileLoader, SourcelessFileLoader
from pkgutil import get_importer
from types import CodeType, ModuleType
import sys
from pathlib import Path

from quux_otelc_python.monitor import Monitor, function_identity
from quux_otelc_python.telemetry import Runtime


def close_application(monitor: Monitor | None, runtime: Runtime) -> None:
    """Flush after application threads and exit callbacks, within the SDK budget."""
    try:
        if monitor is not None:
            monitor.close()
    finally:
        runtime.close()


def execute_main(code: CodeType, filename: str | None, loader: object,
                 spec: ModuleSpec | None = None) -> None:
    """Keep one ordinary application namespace for the remainder of the process."""
    module = ModuleType("__main__")
    module.__dict__.update(__file__=filename, __builtins__=builtins,
                           __cached__=spec.cached if spec else None,
                           __package__=spec.parent if spec else None,
                           __spec__=spec, __loader__=loader)
    # Interpreter-created __main__ has this eager dictionary through 3.13.
    # Seeding it on 3.14 would suppress the compiler's lazy module annotations.
    if sys.version_info < (3, 14):
        module.__annotations__ = {}
    sys.modules["__main__"] = module
    exec(code, module.__dict__)


def module_entry(spec: ModuleSpec | None) -> tuple[ModuleSpec, CodeType]:
    """Require a real code loader, as ordinary CPython module execution does."""
    if spec is None or spec.submodule_search_locations is not None:
        raise ImportError("Python entry point has no executable main module")
    get_code = getattr(spec.loader, "get_code", None)
    code = get_code(spec.name) if get_code else None
    if code is None:
        raise ImportError("Python entry point loader has no executable code")
    return spec, code


def run_module(name: str) -> None:
    """Resolve normal module/package metadata before executing as __main__."""
    spec = importlib.util.find_spec(name)
    if spec is not None and spec.submodule_search_locations is not None:
        spec = importlib.util.find_spec(name + ".__main__")
    spec, code = module_entry(spec)
    sys.argv[0] = spec.origin
    execute_main(code, spec.origin, spec.loader, spec)


def run_script(argument: str) -> None:
    """Use ordinary script metadata while retaining the caller's argv spelling."""
    filename = argument if os.path.isabs(argument) else os.path.join(os.getcwd(), argument)
    if get_importer(filename) is not None:
        sys.path[0] = filename
        spec = PathFinder.find_spec("__main__", [filename])
        spec, code = module_entry(spec)
        execute_main(code, spec.origin, spec.loader, spec)
        return
    bytecode = filename.endswith(".pyc")
    loader = (SourcelessFileLoader if bytecode else SourceFileLoader)("__main__", filename)
    code = loader.get_code("__main__") if bytecode else compile(
        Path(filename).read_bytes(), filename, "exec", dont_inherit=True)
    execute_main(code, filename, loader)


def main(args: list[str]) -> int:
    if len(args) < 2:
        raise ValueError("python requires SCRIPT [ARGS...] or -m MODULE [ARGS...]")
    if args[1] == "-m" and len(args) < 3:
        raise ValueError("-m requires a module name")
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
    deferred = False
    try:
        monitor = Monitor(plan, runtime)
        monitor.install()
        atexit.register(close_application, monitor, runtime)
        deferred = True
        if args[1] == "-m":
            sys.argv = args[2:]
            sys.path[0] = str(Path.cwd())
            run_module(args[2])
        else:
            sys.argv = args[1:]
            sys.path[0] = str(Path(args[1]).resolve().parent)
            run_script(args[1])
    finally:
        if not deferred:
            close_application(monitor, runtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
