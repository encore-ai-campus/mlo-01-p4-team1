"""Exercise required-suite gates with real discovery and subprocess exit codes."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RequiredSuiteTests(unittest.TestCase):
    def run_fixture(self, source=None, pattern="test*.py"):
        with tempfile.TemporaryDirectory() as folder:
            if source is not None:
                Path(folder, "test_fixture.py").write_text(
                    "import unittest\nclass Fixture(unittest.TestCase):\n" + source
                )
            run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mlo_runtime.suite",
                    "--start",
                    folder,
                    "--pattern",
                    pattern,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        summary = json.loads(run.stderr.splitlines()[-1])
        return run.returncode, summary

    def test_empty_discovery_and_missing_required_module_fail(self):
        for source, pattern in [
            (None, "test*.py"),
            (" def test_ok(self): pass\n", "test_missing.py"),
        ]:
            with self.subTest(pattern=pattern):
                code, summary = self.run_fixture(source, pattern)
                self.assertEqual(code, 2)
                self.assertEqual(summary["tests"], 0)

    def test_skip_does_not_turn_partial_coverage_into_pass(self):
        code, summary = self.run_fixture(
            " def test_ok(self): pass\n"
            " @unittest.skip('dependency missing')\n def test_required(self): pass\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(summary["tests"], 2)
        self.assertEqual(summary["skipped"], 1)

    def test_expected_failure_does_not_hide_unfinished_required_test(self):
        code, summary = self.run_fixture(
            " @unittest.expectedFailure\n def test_required(self): self.fail()\n"
        )
        self.assertEqual(code, 2)
        self.assertEqual(summary["expected_failures"], 1)

    def test_assertion_and_import_errors_fail(self):
        for source in [
            " def test_required(self): self.fail()\n",
            " pass\nraise ImportError('missing dependency')\n",
        ]:
            with self.subTest(source=source):
                code, summary = self.run_fixture(source)
                self.assertEqual(code, 2)
                self.assertEqual(summary["errors"] + summary["failures"], 1)

    def test_success_requires_executed_tests(self):
        code, summary = self.run_fixture(" def test_ok(self): pass\n")
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["tests"], 1)
