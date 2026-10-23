"""Real isolated workers compile/decode but never execute the retained program."""

from __future__ import annotations

import __future__
import hashlib
import importlib.util
import marshal
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from otelc.file_inputs import identity
from otelc.python_code_attribution import locate
from otelc.python_code_candidate import Candidate, build
from otelc.python_code_descriptor import encode
from otelc.python_code_index import decode
from otelc.python_input_journal import CodeInput

FILENAME = '/retained/example.py'
SOURCE = b'def workload(value):\n    return value + 42\n'


def captured(source: bytes = SOURCE, **options):
    module = compile(source, FILENAME, 'exec', dont_inherit=True, **options)
    function = next(value for value in module.co_consts if type(value) is type(module))
    return decode(CodeInput(FILENAME.encode(), encode(function), 1, 0, sys.hexversion, 2))


class PythonCodeCandidateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.interpreter = identity(Path(sys.executable))

    def build(self, content=SOURCE, **options):
        return build(self.interpreter, python_version=sys.hexversion, content=content,
                     filename=FILENAME, kind=options.pop('kind', 'source'), **options)

    def test_source_and_nested_function_compare_without_execution_or_reopening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'must-not-exist'
            source = SOURCE + f"open({str(marker)!r}, 'w').write('executed')\n".encode()
            result = self.build(source)
            self.assertEqual(result.status, 'decoded', result)
            found = locate(captured(source), [result])
            self.assertEqual(found.status, 'unique-match')
            self.assertEqual(len(found.matches), 1)
            self.assertFalse(marker.exists())
            self.assertFalse(found.source_complete)
            self.assertEqual(result.evidence['input_sha256'], hashlib.sha256(source).hexdigest())
            self.assertEqual(result.evidence['argv'][1:4], ['-I', '-S', '-B'])

    def test_changed_constants_unmatched_and_duplicate_candidates_ambiguous(self) -> None:
        exact = self.build()
        changed = self.build(SOURCE.replace(b'42', b'43'))
        self.assertEqual(locate(captured(), [changed]).status, 'unmatched')
        result = locate(captured(), [exact, exact])
        self.assertEqual((result.status, len(result.matches)), ('ambiguous', 2))
        failed = Candidate('incomplete', None, {}, 'worker-refused')
        self.assertEqual(locate(captured(), [exact, failed]).status, 'incomplete')
        self.assertEqual(locate(captured(), []).status, 'unmatched')
        self.assertEqual(locate(captured(), [exact], budget=1).status, 'incomplete')
        prepared_input = captured()
        with self.assertRaises(ValueError):
            locate(prepared_input, [], budget=True)

    def test_encoding_bom_crlf_future_flags_and_optimisation_are_explicit(self) -> None:
        sources = (b'\xef\xbb\xbf' + SOURCE.replace(b'\n', b'\r\n'),
                   b'# coding: latin-1\ndef workload():\n    return "caf\xe9"\n')
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(locate(captured(source), [self.build(source)]).status, 'unique-match')
        future = b'def workload(value: Missing):\n    return __debug__\n'
        options = {'flags': __future__.annotations.compiler_flag, 'optimize': 1}
        exact = self.build(future, **options)
        self.assertEqual(locate(captured(future, **options), [exact]).status, 'unique-match')
        self.assertEqual(locate(captured(future, **options), [self.build(future)]).status, 'unmatched')

    def test_pyc_modes_retained_header_and_stale_source_are_separate(self) -> None:
        module = compile(SOURCE, FILENAME, 'exec', dont_inherit=True)
        for flags in (0, 1, 2, 3):
            pyc = importlib.util.MAGIC_NUMBER + flags.to_bytes(4, 'little') + b'12345678' + marshal.dumps(module)
            with self.subTest(flags=flags):
                result = self.build(pyc, kind='pyc')
                self.assertEqual(result.status, 'decoded', result)
                details = result.evidence['result']['details']
                self.assertEqual(details['pyc_flags'], flags)
                self.assertEqual(details['pyc_validation'], 'not-checked')
                self.assertEqual(locate(captured(), [result]).status, 'unique-match')
                self.assertEqual(locate(captured(SOURCE.replace(b'42', b'43')), [result]).status, 'unmatched')

    def test_malformed_pyc_source_and_version_mismatch_refuse_without_fallback(self) -> None:
        header = importlib.util.MAGIC_NUMBER + b'\0' * 12
        module = compile(SOURCE, FILENAME, 'exec', dont_inherit=True)
        for content in (b'', b'wrong header' * 2, header + b'bad',
                        header + marshal.dumps(42), header + marshal.dumps(module) + b'trailer',
                        importlib.util.MAGIC_NUMBER + b'\x04' + b'\0' * 11 + marshal.dumps(module)):
            with self.subTest(content=content[:20]):
                self.assertEqual(self.build(content, kind='pyc').status, 'incomplete')
        self.assertEqual(self.build(b'def invalid(').status, 'incomplete')
        result = build(self.interpreter, python_version=sys.hexversion + 1,
                       content=SOURCE, filename=FILENAME, kind='source')
        self.assertEqual((result.status, result.reason), ('incomplete', 'worker-refused'))
        self.assertEqual(self.build(flags=0x7fffffff).status, 'incomplete')

    def test_identity_timeout_and_crash_fail_closed(self) -> None:
        changed = {**self.interpreter, 'sha256': '0' * 64}
        self.assertEqual(build(changed, python_version=sys.hexversion, content=SOURCE,
            filename=FILENAME, kind='source').reason, 'interpreter-identity')
        with mock.patch('otelc.python_code_candidate.subprocess.run',
                        side_effect=subprocess.TimeoutExpired('worker', 1)):
            self.assertEqual(self.build().reason, 'worker-timeout')
        with mock.patch('otelc.python_code_candidate.subprocess.run',
                        return_value=subprocess.CompletedProcess([], -11)):
            self.assertEqual(self.build().reason, 'worker-refused')
        with mock.patch('otelc.python_code_candidate.subprocess.run',
                        side_effect=OSError('launch refused')):
            self.assertEqual(self.build().reason, 'worker-failure')

    def test_invalid_caller_requests_are_not_worker_results(self) -> None:
        for options in ({'flags': True}, {'optimize': -1}, {'timeout': float('nan')},
                        {'timeout': 0}, {'filename': ''}, {'filename': 'a\0b'},
                        {'content': bytearray(b'x')}, {'kind': 'other'},
                        {'kind': 'pyc', 'optimize': 1}):
            arguments = {'python_version': sys.hexversion, 'content': SOURCE,
                         'filename': FILENAME, 'kind': 'source', **options}
            with self.subTest(options=options), self.assertRaises(ValueError):
                build(self.interpreter, **arguments)


if __name__ == '__main__':
    unittest.main()
