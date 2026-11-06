"""Run real suites; missing dependencies and mandatory tests never become success."""

import argparse
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .contracts import RUNTIME, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("suite", choices=["unit", "integration", "all"])
    args = parser.parse_args()
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = RUNTIME / ".artifacts" / ("checks-" + run_id)
    folder.mkdir(parents=True)
    python = sys.executable
    binary = Path(python).parent
    commands = {}
    if args.suite in {"unit", "all"}:
        commands.update(
            {
                "B-U1-U3": [
                    python,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(RUNTIME / "tests/unit"),
                    "-v",
                ],
                "ansible-syntax": [
                    str(binary / "ansible-playbook"),
                    "-i",
                    "localhost,",
                    "-c",
                    "local",
                    str(RUNTIME / "ansible/site.yml"),
                    "--syntax-check",
                ],
                "ansible-lint": [
                    str(binary / "ansible-lint"),
                    "--offline",
                    str(RUNTIME / "ansible/site.yml"),
                    str(RUNTIME / "ansible/lab.yml"),
                ],
            }
        )
    if args.suite in {"integration", "all"}:
        commands.update(
            {
                "B-I1-I3-process": [
                    python,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(RUNTIME / "tests/integration"),
                    "-p",
                    "test_offline.py",
                    "-v",
                ],
                "B-I2-I4-docker": [
                    python,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(RUNTIME / "tests/integration"),
                    "-p",
                    "test_docker.py",
                    "-v",
                ],
                "B-I3-haproxy-web": [
                    python,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(RUNTIME / "tests/integration"),
                    "-p",
                    "test_proxy_web.py",
                    "-v",
                ],
                "B-I4-spark-delta": [
                    python,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(RUNTIME / "tests/integration"),
                    "-p",
                    "test_spark.py",
                    "-v",
                ],
            }
        )
    results = {}
    if args.suite in {"integration", "all"}:
        for name, pattern in [
            ("B-I5-artifact-install", "test_install.py"),
            ("B-I6-zookeeper-quorum", "test_zookeeper.py"),
            ("B-I7-supervisor", "test_supervisor.py"),
            ("B-I8-relay-tls", "test_relay.py"),
        ]:
            commands[name] = [python, "-m", "unittest", "discover", "-s", str(RUNTIME / "tests/integration"), "-p", pattern, "-v"]
            if not (RUNTIME / "tests/integration" / pattern).is_file():
                raise RuntimeError("mandatory integration suite is missing: " + pattern)
    for name, command in commands.items():
        print("RUN: " + name, flush=True)
        log = folder / (name + ".log")
        try:
            with log.open("w") as stream:
                result = subprocess.run(
                    command,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                    timeout=3600,
                )
            status = "pass" if result.returncode == 0 else "fail"
            code = result.returncode
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log.write_text(
                type(exc).__name__ + ": required tool/environment unavailable\n"
            )
            status, code = "blocked", 2
        results[name] = {
            "status": status,
            "exit_code": code,
            "command": command,
            "log": str(log.relative_to(ROOT)),
        }
        print(f"{name}: {status} ({log})", flush=True)
    # Local suite success does not grant AWS/app acceptance.
    status = "pass" if all(r["status"] == "pass" for r in results.values()) else "fail"
    summary = {
        "suite": args.suite,
        "status": status,
        "ready_for_aws": False,
        "results": results,
        "platform": os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64"),
        "external_acceptance": "not_run: actual apps, 3AZ, SSM/S3/IAM/IMDS/fencing, home GPU",
    }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"{args.suite}: {status}; ready_for_aws=false; summary={folder / 'summary.json'}"
    )
    return 0 if status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
