#!/usr/bin/env python3
"""Run mandatory A/B/C local integration modules with no real AWS credentials."""

import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["test_foundation_lifecycle", "test_coordination", "test_engine_shutdown"]


def main():
    for key in list(os.environ):
        if key.startswith("AWS_"):
            os.environ.pop(key)
    os.environ.update(
        AWS_CONFIG_FILE=os.devnull,
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_EC2_METADATA_DISABLED="true",
        PYTHONDONTWRITEBYTECODE="1",
    )
    sys.dont_write_bytecode = True
    sys.path[:0] = [
        str(ROOT / "tests/integration"),
        str(ROOT / "aws-infra/contracts"),
        str(ROOT / "aws-infra/runtime/src"),
        str(ROOT / "aws-infra/lifecycle/src"),
        str(ROOT / "aws-infra/lifecycle/tests"),
        str(ROOT / "aws-infra/foundation/scripts"),
    ]
    suite = unittest.TestSuite()
    for name in MODULES:
        path = ROOT / "tests/integration" / (name + ".py")
        if not path.is_file():
            print("NOT READY: missing mandatory module " + name, file=sys.stderr)
            return 2
        loaded = unittest.defaultTestLoader.loadTestsFromName(name)
        if not loaded.countTestCases():
            print("NOT READY: empty mandatory module " + name, file=sys.stderr)
            return 2
        suite.addTests(loaded)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {
        "status": "pass" if result.wasSuccessful() and not result.skipped else "fail",
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "local_only": True,
        "ready_for_aws": False,
        "modules": MODULES,
        "real_aws_calls": False,
    }
    folder = ROOT / ".artifacts/lifecycle-contract"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
