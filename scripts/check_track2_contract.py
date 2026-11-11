#!/usr/bin/env python3
"""Mandatory cross-role Track2 consumers; run after Foundation mock integration."""
from pathlib import Path
import os
import subprocess
import sys

if __name__ == "__main__":
    root=Path(__file__).resolve().parents[1]
    env={k:v for k,v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_CONFIG_FILE=os.devnull,AWS_SHARED_CREDENTIALS_FILE=os.devnull,AWS_EC2_METADATA_DISABLED="true",PYTHONDONTWRITEBYTECODE="1")
    sys.exit(subprocess.run([str(root/"aws-infra/runtime/.venv/bin/python"),str(root/"scripts/required_suite.py"),
        "--start",str(root/"tests/integration"),"--pattern","test_track2_contract.py"],cwd=root,env=env).returncode)
