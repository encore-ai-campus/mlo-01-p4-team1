#!/usr/bin/env python3
"""Mandatory suites, zero skips, durable result summary, no AWS credentials."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BASE / "src"), str(BASE / "tests")]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=["unit", "integration", "all"])
    args = parser.parse_args()
    modules = {"unit": ["test_unit", "test_safety", "test_contracts"],
               "integration": ["test_integration", "test_cli_safety", "test_terraform"]}
    chosen = ["unit", "integration"] if args.suite == "all" else [args.suite]
    folder = BASE / ".artifacts" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    results = {}
    for part in chosen:
        for name in modules[part]:
            if not (BASE / "tests" / (name + ".py")).is_file():
                print("NOT READY: missing mandatory suite " + name, file=sys.stderr)
                return 2
        loaded = [unittest.defaultTestLoader.loadTestsFromName(name) for name in modules[part]]
        if any(suite.countTestCases() == 0 for suite in loaded):
            print("NOT READY: empty mandatory suite", file=sys.stderr)
            return 2
        suite = unittest.TestSuite(loaded)
        with (folder / (part + ".log")).open("w") as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        passed = (result.wasSuccessful() and result.testsRun > 0
                  and not result.skipped and not result.expectedFailures)
        results[part] = {"status": "pass" if passed else "fail", "tests": result.testsRun,
                         "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
                         "errors": len(result.errors), "failures": len(result.failures)}
        print(part + ": " + json.dumps(results[part]), flush=True)
        if not passed:
            print((folder / (part + ".log")).read_text()[-6000:], file=sys.stderr)
    import sqlite3
    summary = {"ready_for_aws": False, "suite": args.suite, "python": sys.version.split()[0],
               "sqlite": sqlite3.sqlite_version, "results": results}
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("summary: " + str(folder / "summary.json"))
    return 0 if all(v["status"] == "pass" for v in results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
