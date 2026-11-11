#!/usr/bin/env python3
"""Offline Linux parser/binary probe in a disposable tooling container.

This DOES NOT boot systemd as PID1, start the services, test EBS or grant AWS
acceptance. No privileged container, Docker socket or host cgroup mount is used.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "aws-infra/runtime/src"))
from mlo_runtime.contracts import load_inventory
from mlo_runtime.native import plan
from mlo_runtime.native_releases import lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="mlo-p4-native-tools:20261110")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
    value = plan(inventory)
    for node, config in value["nodes"].items():
        folder = args.output / node
        folder.mkdir()
        for name, body in config["units"].items():
            (folder / name).write_text(body)
    releases = lock(ROOT / "aws-infra/runtime/native-releases.lock.json")["releases"]
    prefix = ["docker", "run", "--platform", "linux/amd64", "--rm", "--hostname", "native-probe", "--add-host", "native-probe:127.0.0.1", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m", "--tmpfs", "/run:rw,nosuid,nodev,size=32m"]
    homes = {}
    for name, item in releases.items():
        home = "/opt/mlo/native/releases/" + name + "-" + item["digest"][:16]
        homes[name] = home
        prefix += ["--mount", "type=bind,src=" + str((args.extracted / name).resolve()) + ",dst=" + home + ",readonly"]
    prefix += ["--mount", "type=bind,src=" + str(args.output.resolve()) + ",dst=/review,readonly"]
    commands = {"systemd-version": ["systemd-analyze", "--version"],
                "java-version": [homes["java21"] + "/bin/java", "-version"],
                "kafka-version": ["env", "JAVA_HOME=" + homes["java21"], "LOG_DIR=/tmp/kafka-logs", homes["kafka"] + "/bin/kafka-topics.sh", "--version"],
                "spark-version": ["env", "JAVA_HOME=" + homes["java21"], "SPARK_LOCAL_DIRS=/tmp/spark", homes["spark"] + "/bin/spark-submit", "--version"],
                "es-version": [homes["elasticsearch"] + "/bin/elasticsearch", "--version"]}
    for node, config in value["nodes"].items():
        commands[node] = ["systemd-analyze", "verify", "--man=no", *["/review/" + node + "/" + n for n in config["units"]]]
    results = {}
    for name, argv in commands.items():
        result = subprocess.run([*prefix, args.image, *argv], capture_output=True, timeout=120)
        (args.output / (name + ".log")).write_bytes(result.stdout + result.stderr)
        results[name] = {"exit_code": result.returncode, "status": "pass" if result.returncode == 0 else "fail"}
        print(name + ": " + results[name]["status"], flush=True)
    report = {"schema_version": "native-linux-probe-v1", "results": results,
              "ready_for_aws": False, "systemd_pid1_tested": False,
              "scope": "Ubuntu 24.04 tooling container; binary amd64 emulation and static systemd parser only"}
    (args.output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if all(v["status"] == "pass" for v in results.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
