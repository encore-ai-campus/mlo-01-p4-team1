#!/usr/bin/env python3
"""Reproducible local native handoff; no AWS calls or deployment approval."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "aws-infra/runtime/src"), str(ROOT / "aws-infra/contracts")]
from mlo_runtime.contracts import load_inventory
from mlo_runtime.native import contract, plan
from mlo_runtime.native_releases import archive_path, file_digest, lock, verify, need
from mlo_runtime.native_package import package
from artifact_verifier import inspect_artifact
from infra_common.preauth_execution import source_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--linux-probe", type=Path, required=True)
    parser.add_argument("--runtime-summary", type=Path, required=True)
    parser.add_argument("--lifecycle-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    need(not args.output.exists() and ".local" in args.output.resolve().parts, "new local review folder required")
    value = lock(ROOT / "aws-infra/runtime/native-releases.lock.json")
    archives = {n: verify(archive_path(args.cache, n, item), item) for n, item in value["releases"].items()}
    probe, runtime, lifecycle = [json.loads(p.read_text()) for p in (args.linux_probe, args.runtime_summary, args.lifecycle_summary)]
    need(probe.get("systemd_pid1_tested") is False and probe.get("results") and all(v["status"] == "pass" for v in probe["results"].values()), "Linux parser/binary probe incomplete")
    need(runtime.get("status") == "pass" and runtime.get("suite") == "unit", "Runtime/Ansible local suite failed")
    need(lifecycle.get("results", {}).get("unit", {}).get("status") == "pass", "Lifecycle local suite failed")
    inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
    candidate = plan(inventory)
    need(len(candidate["nodes"]) == 9, "native cohort incomplete")
    args.output.mkdir(parents=True, mode=0o700)
    artifact = package(inventory, args.output / "native-fixture.tgz", "native-predeploy-fixture", fixture=True)
    for name in artifact["target_nodes"]:
        inspect_artifact(artifact["archive"], artifact["sha256"], inventory["environment"], inventory["generation"], name, args.output / name, artifact["manifest_sha256"])
    result = {"schema_version": "native-predeploy-local-v1", "local_checks": "pass", "ready_for_aws": False,
              "aws_calls": False, "source_sha256": source_hash(), "native_hosts": 9,
              "native_units": sum(len(n["units"]) for n in candidate["nodes"].values()),
              "release_lock_sha256": file_digest(ROOT / "aws-infra/runtime/native-releases.lock.json"),
              "archives": archives, "fixture_artifact": artifact,
              "evidence_files": {str(p): file_digest(p) for p in (args.linux_probe, args.runtime_summary, args.lifecycle_summary)},
              "deployment_blockers": contract()["deployment_blockers"],
              "not_executed": ["AWS apply/upload/SSM", "actual systemd PID1/restart", "real EBS/PKI/remote fencing", "48h load and independent data restore"],
              "next": "accept offline dependencies and real-host/bootstrap/remote integration evidence; regenerate reviewed Terraform plans; never deploy fixture artifact"}
    (args.output / "summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"local_checks": "pass", "ready_for_aws": False, "aws_calls": False, "summary": str(args.output / "summary.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
