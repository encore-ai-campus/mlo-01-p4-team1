#!/usr/bin/env python3
"""Mandatory authentication-free tests; do not discover real credentials."""

import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["test_preauth_boundary", "test_preauth_destroy", "test_preauth_workflows"]


def main():
    for name in list(os.environ):
        if name.startswith("AWS_"):
            del os.environ[name]
    os.environ.update(
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_EC2_METADATA_DISABLED="true",
        PYTHONDONTWRITEBYTECODE="1",
    )
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "tests/integration"))
    suite = unittest.TestSuite()
    for module in MODULES:
        if not (ROOT / "tests/integration" / (module + ".py")).is_file():
            raise RuntimeError("mandatory preauthentication module missing")
        tests = unittest.defaultTestLoader.loadTestsFromName(module)
        if not tests.countTestCases():
            raise RuntimeError("empty preauthentication module")
        suite.addTests(tests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {
        "status": "pass" if result.wasSuccessful() and not result.skipped else "fail",
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "real_aws_calls": False,
        "ready_for_aws": False,
        "modules": MODULES,
    }
    output = ROOT / ".artifacts/preauth"
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
