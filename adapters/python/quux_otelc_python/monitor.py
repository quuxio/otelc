"""CPython monitoring preserves original code, frames, signatures and async behaviour."""
import ast
import inspect
import sys
import tokenize
from pathlib import Path

from .policy import Selection, source_name


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
    def __init__(self, plan: dict, runtime):
        if not hasattr(sys, "monitoring"):
            raise ValueError("Python instrumentation requires CPython 3.12+")
        self.runtime = runtime
        self.plan = plan
        self.root = Path.cwd().resolve()
        self.protected = Path(__file__).resolve().parents[1]
        self.sources = Selection(plan["source_matchers"])
        self.functions = Selection(plan["function_matchers"])
        self.names = {}
        self.tags = {}
        self.tool = None
        if plan["annotations"]["read_existing"]:
            for path in self.root.rglob("*.py"):
                name = source_name(str(path), self.root)
                if name and self.sources.accepts(name) and not path.resolve().is_relative_to(self.protected):
                    self.tags[str(path.resolve())] = annotations(path)

    def display_name(self, code) -> str | None:
        name = source_name(code.co_filename, self.root)
        if not name or not self.sources.accepts(name) or Path(code.co_filename).resolve().is_relative_to(self.protected):
            return None
        if not code.co_flags & inspect.CO_NEWLOCALS:
            return None
        tag = self.tags.get(str(Path(code.co_filename).resolve()), {}).get(code.co_firstlineno)
        if tag == "otelc.exclude":
            return None
        display = name.removesuffix(".py").replace("/", ".") + "." + code.co_qualname
        return display if self.functions.accepts(display, tag == "otelc.instrument") else None

    def start(self, code, _):
        name = self.names.get(code)
        if name is None:
            name = self.display_name(code)
            if name is None:
                return sys.monitoring.DISABLE
            if len(self.names) >= self.plan["runtime"]["max_functions"] or len(name.encode()) > 1024:
                self.runtime.loss["function_capacity"] += 1
                return sys.monitoring.DISABLE
            self.names[code] = name
        self.runtime.enter(id(sys._getframe(1)), name)

    def returned(self, code, _, value):
        if code not in self.names:
            return sys.monitoring.DISABLE
        self.runtime.exit(id(sys._getframe(1)), False)

    def unwound(self, code, _, exception):
        if code in self.names:
            self.runtime.exit(id(sys._getframe(1)), True)

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
        monitoring.set_events(self.tool, events.PY_START | events.PY_RETURN | events.PY_UNWIND)

    def close(self):
        if self.tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        for event in (monitoring.events.PY_START, monitoring.events.PY_RETURN, monitoring.events.PY_UNWIND):
            monitoring.register_callback(self.tool, event, None)
        monitoring.free_tool_id(self.tool)
        self.tool = None
