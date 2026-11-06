"""Validate fixed runtime job bindings before invoking an accepted app interface."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .operations import local_lock, UnsafeTransition
from .queries import (
    QUERIES,
    EXECUTOR_PORTS,
    TRACK2_QUERIES,
    TRACK2_EXECUTOR_PORTS,
    validate_ports,
)

JOBS = {
    "raw-reconcile",
    "behavior-export",
    "feedback-analytics-batch",
    "postgresql-backup",
    "mongodb-backup",
    "weather-collector",
    "ads-observation",
    "venues-catalog",
}


def spark_submit(query, app, masters, driver_ip, *, accepted_artifact=None):
    registry = QUERIES
    executors = EXECUTOR_PORTS
    if query in TRACK2_QUERIES:
        from .track2 import validate_artifact

        if accepted_artifact is None:
            raise UnsafeTransition("Track2 artifact acceptance required")
        validate_artifact(accepted_artifact, query=query, entrypoint=app)
        registry = {**QUERIES, **TRACK2_QUERIES}
        executors = {**EXECUTOR_PORTS, **TRACK2_EXECUTOR_PORTS}
    validate_ports()
    if (
        query not in registry
        or not str(app).startswith("/opt/mlo/current/apps/")
        or ".." in Path(app).parts
    ):
        raise UnsafeTransition("unaccepted Spark app path/query")
    driver, block = registry[query]
    command = [
        "/opt/spark/bin/spark-submit",
        "--master",
        "spark://" + ",".join(masters),
        "--deploy-mode",
        "client",
        "--conf",
        "spark.driver.host=" + driver_ip,
        "--conf",
        "spark.driver.bindAddress=" + driver_ip,
        "--conf",
        f"spark.driver.port={driver}",
        "--conf",
        f"spark.blockManager.port={block}",
        "--conf",
        f"spark.executor.port={executors[query]}",
        "--conf",
        "spark.ui.enabled=false",
        "--conf",
        "spark.port.maxRetries=0",
    ]
    if query in TRACK2_QUERIES:
        command += [
            "--driver-memory",
            "1g",
            "--executor-memory",
            "1g",
            "--executor-cores",
            "1",
            "--total-executor-cores",
            "2",
        ]
    return [*command, str(app)]


def validate_job(job, binding_path):
    if job not in JOBS:
        raise UnsafeTransition("unknown job")
    binding_path = Path(binding_path)
    if binding_path.stat().st_mode & 0o077:
        raise UnsafeTransition("job binding permissions must be 0600")
    binding = json.loads(binding_path.read_text())
    if binding.get("example_only") is not False or binding.get("ready") is not True:
        raise UnsafeTransition("job inputs are unresolved")
    item = binding["jobs"][job]
    executable = Path(item["executable"])
    if not executable.is_absolute() or not executable.resolve().is_relative_to(
        Path("/opt/mlo/current").resolve()
    ):
        raise UnsafeTransition("job entrypoint outside accepted versioned artifact")
    if hashlib.sha256(executable.read_bytes()).hexdigest() != item["sha256"]:
        raise UnsafeTransition("job entrypoint checksum mismatch")
    if job in {"feedback-analytics-batch", "raw-reconcile", "behavior-export", "venues-catalog"}:
        # These timers enqueue into the single fenced active group; they may not submit work themselves.
        if item.get("mode") != "enqueue-active-group":
            raise UnsafeTransition(
                "Control timers must enqueue to the active execution group"
            )
    if job == "venues-catalog":
        from .track2 import validate_artifact, accept_foundation
        accepted = item.get("artifact")
        if not isinstance(accepted, dict):
            raise UnsafeTransition("accepted Track2 app manifest required")
        validate_artifact(accepted, query=job, entrypoint=accepted.get("entrypoint"))
        bridge = accept_foundation(binding.get("foundation_track2", {}), binding.get("inventory", {}))
        if bridge["dataset_version"] != accepted["dataset_version"]:
            raise UnsafeTransition("app/Foundation dataset mismatch")
    if job == "weather-collector" and item.get("manual_active_confirmed") is not True:
        raise UnsafeTransition(
            "collector takeover requires manual old collector stop confirmation"
        )
    if job == "ads-observation" and not (
        item.get("mode") == "accepted-read-only-projection"
        and item.get("projection_contract_accepted") is True
        and item.get("source_read_only") is True
        and item.get("original_ledger_preserved") is True
    ):
        raise UnsafeTransition("accepted read-only observation artifact required")
    arguments = item.get("arguments", [])
    if not all(isinstance(a, str) for a in arguments):
        raise UnsafeTransition("job argv must be a string array")
    return executable, arguments


def run_job(job, binding_path, *, maintenance_barrier=None):
    if job == "venues-catalog":
        # Admission validation is available for handoff, but the actual app and
        # whole-cluster stop/drain adapter have not been accepted. A ready flag
        # in a job JSON must never enable a query the supervisor cannot fence.
        raise UnsafeTransition("Track2 operational app/whole-group stop hooks are not accepted")
    executable, arguments = validate_job(job, binding_path)
    if maintenance_barrier is None:
        from .contracts import ROOT
        import sys

        sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
        from infra_common.factory import load_guard
        from infra_common.coordination import MaintenanceBarrier

        binding = json.loads(Path(binding_path).read_text())
        if not binding.get("coordination_contract") or not binding.get(
            "coordination_scope"
        ):
            raise UnsafeTransition(
                "accepted maintenance barrier required for timer/collector"
            )
        common = load_guard(
            binding["coordination_contract"],
            binding["coordination_scope"],
            "job-" + job,
            "deploy",
        )
        maintenance_barrier = MaintenanceBarrier(common.store, common.scope)
    maintenance_barrier.check()
    with local_lock("/run/mlo/locks/" + job + ".lock"):
        maintenance_barrier.check()
        subprocess.run([str(executable), *arguments], check=True, timeout=840)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", choices=sorted(JOBS), required=True)
    parser.add_argument("--binding", type=Path, default=Path("/etc/mlo/jobs.json"))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        validate_job(args.job, args.binding)
    else:
        run_job(args.job, args.binding)


if __name__ == "__main__":
    main()
