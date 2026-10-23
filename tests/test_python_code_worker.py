"""Exercise the worker protocol independently of the owning subprocess API."""

from __future__ import annotations

import io
import json
import sys
import unittest
from unittest import mock

from otelc import python_code_worker as worker


class PythonCodeWorkerTests(unittest.TestCase):
    def run_worker(self, header=None, content=b'value = 42\n', *, raw=None):
        request = {'kind': 'source', 'filename': '/retained/example.py', 'flags': 0,
                   'optimize': 0, 'python_version': sys.hexversion, **(header or {})}
        data = json.dumps(request).encode() + b'\n' + content if raw is None else raw
        output = io.BytesIO()
        with mock.patch.object(worker.sys, 'stdin', mock.Mock(buffer=io.BytesIO(data))), \
             mock.patch.object(worker.sys, 'stdout', mock.Mock(buffer=output)), \
             mock.patch.object(worker.sys, 'stderr', io.StringIO()), \
             mock.patch.object(worker.resource, 'setrlimit') as limits:
            result = worker.main()
            self.assertEqual(limits.call_count, 3)
        return result, output.getvalue()

    def test_valid_request_retains_compiler_parameters_and_descriptor(self) -> None:
        status, payload = self.run_worker({'optimize': 2})
        self.assertEqual(status, 0)
        header, descriptor = payload.split(b'\n', 1)
        result = json.loads(header)
        self.assertEqual(result['descriptor_size'], len(descriptor))
        self.assertEqual(result['details'], {'compile_flags': 0, 'optimize': 2})
        self.assertEqual(json.loads(descriptor)['schema_version'], 2)

    def test_incomplete_or_wrong_profile_requests_refuse(self) -> None:
        for raw in (b'{}', b'null\n', b'{}\n', b'{\n'):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_worker(raw=raw)[0], 2)
        for change in ({'kind': 'other'}, {'filename': ''}, {'filename': 'a\0b'},
                       {'flags': True}, {'optimize': -1}, {'python_version': True},
                       {'python_version': sys.hexversion + 1}, {'unknown': 1},
                       {'kind': 'pyc', 'flags': 1}):
            with self.subTest(change=change):
                self.assertEqual(self.run_worker(change)[0], 2)

    def test_request_input_output_limits_and_non_code_compiler_flags_refuse(self) -> None:
        with mock.patch.object(worker, 'MAX_HEADER', 1):
            self.assertEqual(self.run_worker()[0], 2)
        with mock.patch.object(worker, 'MAX_SOURCE', 1):
            self.assertEqual(self.run_worker()[0], 2)
            self.assertEqual(self.run_worker(content=b'')[0], 2)
        # PyCF_ONLY_AST must not turn a successful compile into a code receipt.
        import ast
        self.assertEqual(self.run_worker({'flags': ast.PyCF_ONLY_AST})[0], 2)


if __name__ == '__main__':
    unittest.main()
