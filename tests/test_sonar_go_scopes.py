"""Every owned Go test family must be indexed as tests in Sonar."""
from pathlib import Path
import unittest

from otelc.config import Selection


class SonarGoScopeTests(unittest.TestCase):
    def test_all_owned_go_test_files_have_source_root_and_test_inclusion(self) -> None:
        root = Path(__file__).resolve().parents[1]
        properties = dict(line.split('=', 1) for line in
            (root / 'sonar-project.properties').read_text().splitlines()
            if line and not line.startswith('#'))
        test_roots = tuple(Path(value) for value in properties['sonar.tests'].split(','))
        # The configured Ant-style subset uses the same slash-aware * / **
        # semantics as the common policy, including zero directories for **/.
        includes = Selection(tuple(properties['sonar.test.inclusions'].split(',')), (), paths=True)
        count = 0
        for project in sorted((root / 'adapters').glob('go*')):
            if not (project / 'go.mod').is_file():
                continue
            for test in sorted(project.rglob('*_test.go')):
                relative = test.relative_to(root)
                with self.subTest(path=relative.as_posix()):
                    self.assertTrue(any(relative.is_relative_to(parent) for parent in test_roots))
                    self.assertTrue(includes.accepts(relative.as_posix()),
                                    'owned Go test is excluded from Sonar test analysis')
                count += 1
        self.assertGreater(count, 0, 'there are no owned Go tests to classify')
