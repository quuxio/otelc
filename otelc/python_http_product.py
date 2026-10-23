"""Source-bound official HTTP products with narrow behaviour-preservation fixes."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

SOURCES = {
    'urllib': '50bd972daf80e168b35f741aef2694bd4b8cf75eed313d1ccfdd070e7939b5dc',
    'wsgi': '776960092571048a4e242928dc437fa1e2574337f2602b3775cbe7e9c3b9da5a',
}


class Preservation(ast.NodeTransformer):
    """Retain upstream HTTP/context logic; remove extra application hook calls."""

    def __init__(self, name: str) -> None:
        self.name = name

    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id == '__name__':
            return ast.copy_location(ast.Constant('opentelemetry.instrumentation.' + self.name), node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        if self.name == 'wsgi' and node.name == '_end_span_after_iterating':
            node.body = ast.parse('return _otelc_response(iterable, span, token)').body
            return node
        node = self.generic_visit(node)
        if self.name == 'urllib' and node.name == 'instrumented_open':
            node.args.defaults[-1] = ast.Name('_otelc_open_timeout', ast.Load())
            node.body[:0] = ast.parse('''if not _otelc_request_input(fullurl):
    return opener_open(opener, fullurl, data=data, timeout=timeout)
''').body
        if self.name == 'urllib' and node.name == '_instrumented_open_call':
            lease = ast.parse('with _otelc_transport_context() as _otelc_transport:\n    pass').body[0]
            lease.body = node.body
            node.body = [lease]
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == '_get_opentelemetry_stability_opt_in_mode':
                return ast.copy_location(ast.Attribute(ast.Name('_StabilityMode', ast.Load()), 'HTTP', ast.Load()), node)
            if (isinstance(node.func.value, ast.Name) and node.func.value.id == 'trace'
                    and node.func.attr == 'use_span'):
                node.func = ast.Name('_otelc_use_span', ast.Load())
            elif (isinstance(node.func.value, ast.Name) and node.func.value.id == 'tracer'
                  and node.func.attr == 'start_as_current_span'):
                node.func = ast.Name('_otelc_current_span', ast.Load())
                node.args.insert(0, ast.Name('tracer', ast.Load()))
            elif node.func.attr == 'with_traceback' and isinstance(node.func.value, ast.Name):
                return node.func.value
        if (self.name == 'wsgi' and isinstance(node.func, ast.Name) and node.func.id == 'str'
                and len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == 'ex'):
            node.func = ast.Name('_otelc_exception_type', ast.Load())
        if self.name == 'urllib':
            if isinstance(node.func, ast.Name) and node.func.id == 'inject':
                node.func = ast.Attribute(ast.Name('_otelc_transport', ast.Load()), 'inject', ast.Load())
            text = ast.unparse(node)
            if text == "int(result.headers.get('Content-Length', 0))":
                return ast.copy_location(ast.parse('_otelc_content_length(result)').body[0].value, node)
            if text == "getattr(exc, 'file', None)":
                return ast.copy_location(ast.parse('_otelc_error_result(exc)').body[0].value, node)
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        node = self.generic_visit(node)
        if self.name == 'urllib' and ast.unparse(node) == '0 if data is None else len(data)':
            return ast.copy_location(ast.parse('_otelc_request_size(data)').body[0].value, node)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        node = self.generic_visit(node)
        if (self.name == 'urllib' and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'response_size' and isinstance(node.value, ast.Constant)
                and node.value.value == 0):
            node.value = ast.Constant(None)
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        node = self.generic_visit(node)
        call = node.value
        if (self.name == 'urllib' and isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == 'record' and call.args and isinstance(call.args[0], ast.Name)
                and call.args[0].id in ('request_size', 'response_size')):
            condition = ast.Compare(call.args[0], [ast.IsNot()], [ast.Constant(None)])
            return ast.copy_location(ast.If(condition, [node], []), node)
        return node


def load(name: str, directory: Path, helpers: dict[str, Any]) -> tuple[ModuleType, dict[str, str]]:
    """Reject an unqualified upstream source; retain actual generated code bytes."""
    upstream = importlib.import_module('opentelemetry.instrumentation.' + name)
    source = Path(upstream.__file__).read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != SOURCES[name]:
        raise ValueError('unqualified official Python HTTP source: ' + name)
    tree = Preservation(name).visit(ast.parse(source))
    generated = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
    path = directory / (name + '.py')
    path.write_text(generated)
    module = ModuleType('otelc._http_' + name)
    module.__file__ = str(path)
    module.__dict__.update(helpers)
    exec(compile(generated, str(path), 'exec'), module.__dict__)
    receipt = {'upstream_path': str(upstream.__file__), 'upstream_sha256': digest,
               'product_path': str(path), 'product_sha256': hashlib.sha256(generated.encode()).hexdigest()}
    return module, receipt


def products(directory: Path, helpers: dict[str, Any]) -> tuple[ModuleType, ModuleType]:
    """Keep original and source-bound instrumentors separate in sys.modules."""
    directory.mkdir(mode=0o700)
    client, client_receipt = load('urllib', directory, helpers)
    server, server_receipt = load('wsgi', directory, helpers)
    (directory / 'receipt.json').write_text(json.dumps({
        'schema_version': 1, 'upstream_version': '0.66b1',
        'products': {'urllib': client_receipt, 'wsgi': server_receipt}}, indent=2) + '\n')
    return client, server
