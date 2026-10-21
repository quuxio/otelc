"""CPython monitoring preserves original code, frames, signatures and async behaviour."""
import ast
import asyncio
import inspect
import sys
import tokenize
from pathlib import Path

from .metadata import text
from .policy import Selection, source_name


def function_identity(source: str, code) -> str:
    display = source.removesuffix(".py").replace("/", ".") + "." + text(code.co_qualname)
    if text(code.co_name).startswith("<"):
        column = next((position[2] for position in code.co_positions() if position[2]), 0)
        display += f"@{code.co_firstlineno}:{column + 1}"
    return display


def annotations(path: Path) -> dict[int, str]:
    with tokenize.open(path) as source:
        text = source.read()
    lines = text.splitlines()
    result = {}
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
        index = first - 2
        while index >= 0 and (not lines[index].strip() or lines[index].lstrip().startswith("#")):
            line = lines[index].strip().removeprefix("#").strip()
            if line.startswith("otelc."):
                if line not in ("otelc.instrument", "otelc.exclude"):
                    raise ValueError(f"unsupported otelc annotation in {path}:{index + 1}")
                if result.get(first) != "otelc.exclude":
                    result[first] = line
            index -= 1
    return result


class Monitor:
    def __init__(self, plan: dict, runtime, *, witness=None):
        if not hasattr(sys, "monitoring"):
            raise ValueError("Python instrumentation requires CPython 3.12+")
        self.runtime = runtime
        self.witness = witness
        self.continuations = None
        if witness is not None:
            from .witness_continuations import WitnessContinuations
            self.continuations = WitnessContinuations(witness)
        self.plan = plan
        self.root = Path.cwd().resolve()
        self.protected = Path(__file__).resolve().parents[1]
        self.sources = Selection(plan["source_matchers"])
        self.functions = Selection(plan["function_matchers"])
        self.names = {}
        self.tags = {}
        self.tool = None
        self.context = None
        self.http = None
        if plan["annotations"]["read_existing"]:
            for path in self.root.rglob("*.py"):
                name = source_name(str(path), self.root)
                if name and self.sources.accepts(name) and not path.resolve().is_relative_to(self.protected):
                    self.tags[str(path.resolve())] = annotations(path)

    def display_name(self, code) -> str | None:
        filename = text(code.co_filename)
        name = source_name(filename, self.root)
        if not name or not self.sources.accepts(name) or Path(filename).resolve().is_relative_to(self.protected):
            return None
        if not code.co_flags & inspect.CO_NEWLOCALS:
            return None
        tag = self.tags.get(str(Path(filename).resolve()), {}).get(code.co_firstlineno)
        if tag == "otelc.exclude":
            return None
        display = function_identity(name, code)
        return display if self.functions.accepts(display, tag == "otelc.instrument") else None

    def called(self, _code, _offset, function, _argument):
        if self.continuations is not None:
            self.continuations.called(sys._getframe(1), function)

    def start(self, code, _):
        if self.continuations is not None:
            self.continuations.execution(sys._getframe(1), code)
        witness_event = self.witness.entered(sys._getframe(1)) if self.witness is not None else None
        if not self.runtime.observing or self.runtime.closed:
            return
        if self.lifetime_code(code):
            self.runtime.lifetimes.start(sys._getframe(1), code, self.root)
        entry = self.names.get(id(code))
        if entry is None:
            name = self.display_name(code)
            if name is None:
                return None if (self.lifetime_code(code) or self.witness is not None and self.witness.watches(code)) else sys.monitoring.DISABLE
            if len(self.names) >= self.plan["runtime"]["max_functions"] or len(name.encode()) > 1024:
                if self.runtime.traces is not None:
                    self.runtime.reject_observation("function_capacity", self.parent_key(sys._getframe(1)),
                                                    self.parent_context())
                    return None
                self.runtime.loss["function_capacity"] += 1
                return sys.monitoring.DISABLE
            # Code equality ignores filenames. Retain the object so its identity
            # cannot be reused while this bounded selection cache holds it.
            self.names[id(code)] = (code, name)
        else:
            name = entry[1]
        frame = sys._getframe(1)
        key = id(frame)
        witness_root = witness_event is not None and witness_event.kind == 'wrapper_enter'
        self.runtime.enter(key, name, None if witness_root else self.parent_key(frame),
                           None if witness_root else self.parent_context())
        if self.witness is not None:
            self.witness.admitted(frame, witness_event)
        if self.context is not None:
            with self.runtime.lock:
                pending = self.runtime.pending.get(key)
                if pending is not None:
                    self.context.enter(key, pending.trace)

    def parent_context(self):
        parent = self.context.current.get() if self.context else None
        return parent if parent is not None else self.http.current() if self.http else None

    def parent_key(self, frame):
        parent = None
        if self.runtime.traces is not None:
            ancestor = frame.f_back
            # Inspect active caller links without retaining application frames.
            # Task submission separates logical contexts, including eager calls.
            for _ in range(4096):
                if ancestor is None:
                    break
                if (self.context is not None and self.context.boundary(ancestor.f_code)
                        or self.http is not None and self.http.boundary(ancestor.f_code)):
                    break
                if id(ancestor.f_code) in self.names or self.display_name(ancestor.f_code) is not None:
                    parent = id(ancestor)
                    break
                ancestor = ancestor.f_back
            else:
                parent = 0  # Suppress a tree whose parent search exceeded its bound.
                with self.runtime.lock:
                    self.runtime.traces.reject(None, "parent_depth")
        return parent

    def lifetime_code(self, code):
        if self.runtime is None or self.runtime.lifetimes is None:
            return False
        releases = self.runtime.lifetimes.releases
        method = text(code.co_name)
        if method != '__init__' and (releases is None or method not in releases.methods):
            return False
        filename = text(code.co_filename)
        name = source_name(filename, self.root)
        return bool(name and self.sources.accepts(name) and not Path(filename).resolve().is_relative_to(self.protected))

    def returned(self, code, _, value):
        if self.lifetime_code(code):
            frame = sys._getframe(1)
            with self.runtime.lock:
                pending = self.runtime.pending.get(id(frame))
                if pending is None:
                    pending = self.runtime.pending.get(self.parent_key(frame))
                parent = pending.trace if pending is not None else self.parent_context()
                self.runtime.lifetimes.returned(frame, code, value, parent)
        if id(code) not in self.names:
            if self.witness is not None and self.witness.watches(code):
                self.witness.exited(sys._getframe(1), 'return')
                return None
            # A selected frame may have started while monitoring was off.
            # Disabling its return location would also silence later admitted
            # invocations of that same code after live enable.
            return sys.monitoring.DISABLE if self.runtime.observing and self.display_name(code) is None and not self.lifetime_code(code) else None
        key = id(sys._getframe(1))
        if self.context is not None:
            self.context.suspend(key)
        self.runtime.exit(key, False)
        if self.witness is not None:
            self.witness.exited(sys._getframe(1), 'return')

    def unwound(self, code, _, exception):
        if self.lifetime_code(code):
            self.runtime.lifetimes.unwound(sys._getframe(1))
        if id(code) in self.names:
            key = id(sys._getframe(1))
            if self.context is not None:
                self.context.suspend(key)
            # isinstance may consult an application's exception.__class__ getter.
            self.runtime.exit(key, True, issubclass(type(exception), (GeneratorExit, asyncio.CancelledError)))
        if self.witness is not None:
            self.witness.exited(sys._getframe(1), 'exception')

    def yielded(self, code, _, value):
        if self.continuations is not None:
            self.continuations.execution(sys._getframe(1), code)
        if self.context is not None:
            self.context.suspend(id(sys._getframe(1)))

    def resumed(self, code, _):
        if self.continuations is not None:
            self.continuations.execution(sys._getframe(1), code)
        if self.context is not None:
            self.context.resume(id(sys._getframe(1)))

    def thrown(self, code, _, exception):
        if self.continuations is not None:
            self.continuations.execution(sys._getframe(1), code)
        if self.context is not None:
            self.context.resume(id(sys._getframe(1)))

    def handled_http_exception(self, code, _, exception):
        if self.http is not None:
            try:
                self.http.handled(code, exception)
            except Exception:
                self.http.loss('http_hook')

    def install(self):
        monitoring = sys.monitoring
        for tool in (3, 4, 2, 5):
            if monitoring.get_tool(tool) is None:
                monitoring.use_tool_id(tool, "quux.otelc")
                self.tool = tool
                break
        if self.tool is None:
            raise ValueError("no free CPython monitoring tool ID")
        events = monitoring.events
        monitoring.register_callback(self.tool, events.PY_START, self.start)
        monitoring.register_callback(self.tool, events.PY_RETURN, self.returned)
        monitoring.register_callback(self.tool, events.PY_UNWIND, self.unwound)
        if self.plan.get("propagation", {}).get("tasks", False):
            from .task_context import TaskContext
            if self.runtime.traces is None:
                raise ValueError("Python task propagation requires tracing")
            self.context = TaskContext(self.runtime)
            self.context.install()
            monitoring.register_callback(self.tool, events.PY_YIELD, self.yielded)
            monitoring.register_callback(self.tool, events.PY_RESUME, self.resumed)
            monitoring.register_callback(self.tool, events.PY_THROW, self.thrown)
        if self.continuations is not None:
            monitoring.register_callback(self.tool, events.CALL, self.called)
            monitoring.register_callback(self.tool, events.PY_YIELD, self.yielded)
            monitoring.register_callback(self.tool, events.PY_RESUME, self.resumed)
            monitoring.register_callback(self.tool, events.PY_THROW, self.thrown)
        self.runtime.monitor = self
        if self.plan.get('propagation', {}).get('http', False):
            from .http_context import HTTPContext
            if self.runtime.traces is None:
                self.close()
                raise ValueError('Python HTTP propagation requires tracing')
            try:
                self.http = HTTPContext(self.runtime)
                monitoring.register_callback(self.tool, events.EXCEPTION_HANDLED, self.handled_http_exception)
                self.http.install()
            except BaseException:
                self.close()
                raise
        self.refresh()

    def refresh(self):
        if self.tool is None:
            return
        events = sys.monitoring.events
        mask = events.PY_START | events.PY_RETURN | events.PY_UNWIND if self.runtime.observing else (
            events.PY_RETURN | events.PY_UNWIND if self.runtime.pending else 0)
        if self.context is not None and self.runtime.observing:
            mask |= events.PY_YIELD | events.PY_RESUME | events.PY_THROW
        if self.http is not None and self.runtime.observing:
            mask |= events.EXCEPTION_HANDLED
        if self.continuations is not None:
            mask |= (events.CALL | events.PY_START | events.PY_RETURN | events.PY_UNWIND
                     | events.PY_YIELD | events.PY_RESUME | events.PY_THROW)
        sys.monitoring.set_events(self.tool, mask)

    def close(self):
        if self.tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        if self.http is not None:
            self.http.close()
            self.http = None
        if self.context is not None:
            self.context.close()
            self.context = None
        for event in (monitoring.events.PY_START, monitoring.events.PY_RETURN, monitoring.events.PY_UNWIND,
                      monitoring.events.PY_YIELD, monitoring.events.PY_RESUME, monitoring.events.PY_THROW,
                      monitoring.events.EXCEPTION_HANDLED, monitoring.events.CALL):
            monitoring.register_callback(self.tool, event, None)
        monitoring.free_tool_id(self.tool)
        self.tool = None
        self.runtime.monitor = None
        if self.witness is not None:
            self.witness.close()
