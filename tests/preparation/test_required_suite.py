"""Required coverage gates use actual subprocess discovery results."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class RequiredSuiteTests(unittest.TestCase):
    def execute(self, source):
        with tempfile.TemporaryDirectory() as folder:
            if source is not None:
                Path(folder, 'test_fixture.py').write_text(source)
            return subprocess.run([sys.executable, str(ROOT/'scripts/required_suite.py'),
                                   '--start', folder], capture_output=True, text=True)

    def test_required_suite_rejects_empty_skipped_expected_failure_and_errors(self):
        cases = [None, 'import missing_required_dependency\n',
                 'import unittest\nclass Test(unittest.TestCase):\n def test_required(self): self.fail()\n',
                 'import unittest\nclass Test(unittest.TestCase):\n @unittest.skip("missing")\n def test_required(self): pass\n',
                 'import unittest\nclass Test(unittest.TestCase):\n @unittest.expectedFailure\n def test_required(self): self.fail()\n']
        for source in cases:
            with self.subTest(source=source):
                self.assertNotEqual(self.execute(source).returncode, 0)

    def test_real_test_is_required_for_success(self):
        result = self.execute('import unittest\nclass Test(unittest.TestCase):\n def test_required(self): pass\n')
        self.assertEqual(result.returncode, 0, result.stderr)
