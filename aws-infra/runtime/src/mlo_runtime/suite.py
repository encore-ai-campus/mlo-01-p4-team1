"""Run required unittest suites without accepting empty or skipped coverage."""

import argparse
import json
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(args.start, pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = (
        result.wasSuccessful()
        and result.testsRun > 0
        and not result.skipped
        and not result.expectedFailures
    )
    print(
        json.dumps(
            {
                "status": "pass" if passed else "fail",
                "tests": result.testsRun,
                "skipped": len(result.skipped),
                "expected_failures": len(result.expectedFailures),
                "failures": len(result.failures),
                "errors": len(result.errors),
            },
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
