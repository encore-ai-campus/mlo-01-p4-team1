#!/usr/bin/env python3
"""Require both implementations, then run the real offline execution contract suites."""
from pathlib import Path
import os
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    required = ["aws-infra/foundation/scripts/credential_bridge.py", "aws-infra/foundation/scripts/dispatcher.py", "aws-infra/runtime/src/mlo_runtime/install.py", "aws-infra/runtime/src/mlo_runtime/packager.py", "aws-infra/runtime/.venv/bin/python", "tests/integration/test_execution_contract.py"]
    missing = [p for p in required if not (root / p).is_file()]
    if missing:
        print("NOT READY: missing execution implementation: " + ", ".join(missing), file=sys.stderr)
        return 2
    environment = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    environment.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull, AWS_EC2_METADATA_DISABLED="true", PYTHONDONTWRITEBYTECODE="1")
    environment["PYTHONPATH"] = os.pathsep.join([str(root / "aws-infra/runtime/src"), str(root / "aws-infra/foundation/scripts")])
    return subprocess.run([str(root / "aws-infra/runtime/.venv/bin/python"), "-m", "unittest", "discover", "-s", str(root / "tests/integration"), "-p", "test_execution_contract.py", "-v"], cwd=root, env=environment, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
