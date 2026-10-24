"""Native coverage cannot substitute a different file or an aggregate floor."""
import copy
from pathlib import Path
import unittest

from ci.check_native_controls import summaries


class NativeControlsCoverageTests(unittest.TestCase):
    def test_each_exact_implementation_file_requires_its_own_floor(self) -> None:
        first = Path('/native/first.cpp')
        second = Path('/native/second.cpp')
        def file(path, percent=90, count=10):
            return {'filename': str(path), 'summary': {'lines': {'percent': percent, 'count': count}}}
        data = {'data': [{'files': [file(first), file(second), file('/unrelated.cpp')]}]}
        self.assertEqual(set(summaries(data, {first, second})), {str(first), str(second)})
        for percent, count in [(79.99, 10), (90, 0)]:
            altered = copy.deepcopy(data)
            altered['data'][0]['files'][1] = file(second, percent, count)
            with self.subTest(percent=percent, count=count), self.assertRaisesRegex(ValueError, 'below 80'):
                summaries(altered, {first, second})
        for files in ([file(first)], [file(first), file(first)], [file('/foreign.cpp'), file(second)]):
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, 'exact source once'):
                summaries({'data': [{'files': files}]}, {first, second})
