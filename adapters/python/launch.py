"""External launch adapter: quux-otelc python SCRIPT or python -m MODULE."""
import atexit
import builtins
import json
import inspect
import os
from importlib.machinery import BuiltinImporter, SourceFileLoader, SourcelessFileLoader
from pkgutil import get_importer
import runpy
from types import CodeType, ModuleType
import sys
from pathlib import Path

from quux_otelc_python.monitor import Monitor, function_identity
from quux_otelc_python.telemetry import Runtime


_APPLICATION_STARTED = False


def close_application(monitor: Monitor | None, runtime: Runtime) -> None:
    """Flush after application threads and exit callbacks, within the SDK budget."""
    try:
        if monitor is not None:
            monitor.close()
    finally:
        runtime.close()


def application_main(filename: str | None = None, loader: object = BuiltinImporter) -> ModuleType:
    """Prepare a persistent application namespace before its code is loaded."""
    module = ModuleType("__main__")
    module.__dict__.update(__builtins__=builtins, __loader__=loader)
    if filename is not None:
        module.__dict__.update(__file__=filename, __cached__=None)
    # Interpreter-created __main__ has this eager dictionary through 3.13.
    # Seeding it on 3.14 would suppress the compiler's lazy module annotations.
    if sys.version_info < (3, 14):
        module.__annotations__ = {}
    sys.modules["__main__"] = module
    return module


def execute_main(code: CodeType, module: ModuleType) -> None:
    """Execute original script code in the namespace which survives shutdown."""
    exec(code, module.__dict__)


def run_module(name: str) -> None:
    """Use CPython's own module entry route, including its traceback frames."""
    application_main()
    runpy._run_module_as_main(name)


def run_script(argument: str) -> None:
    """Use ordinary script metadata while retaining the caller's argv spelling."""
    filename = argument if os.path.isabs(argument) else os.path.join(os.getcwd(), argument)
    if get_importer(filename) is not None:
        sys.path[0] = filename
        application_main()
        runpy._run_module_as_main("__main__", alter_argv=False)
        return
    bytecode = filename.endswith(".pyc")
    loader = (SourcelessFileLoader if bytecode else SourceFileLoader)("__main__", filename)
    module = application_main(filename, loader)
    code = loader.get_code("__main__") if bytecode else compile(
        Path(filename).read_bytes(), filename, "exec", dont_inherit=True)
    execute_main(code, module)


def trim_launch_prefix(error: BaseException, entry_codes: tuple[CodeType, ...]) -> None:
    """Restore the application's traceback without dispatching exception getters."""
    descriptor = BaseException.__dict__['__traceback__']
    traceback = descriptor.__get__(error, type(error))
    while traceback is not None and any(traceback.tb_frame.f_code is code for code in entry_codes):
        traceback = traceback.tb_next
    descriptor.__set__(error, traceback)


def main(args: list[str]) -> int:
    global _APPLICATION_STARTED
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
                boundary = plan['lifetimes']['boundary']
                signals += f" and ordinary own-initializer weak-referenceable class {boundary} lifetimes"
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
        _APPLICATION_STARTED = True
        if args[1] == "-m":
            sys.argv = ["-m", *args[3:]]
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
    entry_codes = (sys._getframe().f_code, main.__code__, run_script.__code__,
                   run_module.__code__, execute_main.__code__, application_main.__code__)
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BaseException as error:
        if _APPLICATION_STARTED:
            trim_launch_prefix(error, entry_codes)
        raise
