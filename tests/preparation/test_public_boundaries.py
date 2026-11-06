"""Staged local-only material must be rejected even after a forced add."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicBoundaryTests(unittest.TestCase):
    def scan(self, name):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copyfile(ROOT / "scripts/check_public_release.py", root / "scripts/check_public_release.py")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@users.noreply.github.com"], cwd=root, check=True)
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic fixture only\n")
            subprocess.run(["git", "add", "-f", "--", name], cwd=root, check=True)
            result = subprocess.run([sys.executable, "scripts/check_public_release.py", "--staged"], cwd=root, capture_output=True)
            return result.returncode

    def test_forced_local_material_is_rejected(self):
        for path in ["AGENTS.md", "aws-infra/foundation/AGENTS.md", "AGENTS.override.md", ".local/report.txt", "archive.bundle"]:
            with self.subTest(path=path):
                self.assertNotEqual(self.scan(path), 0)

    def test_public_source_is_allowed(self):
        self.assertEqual(self.scan("README.md"), 0)
