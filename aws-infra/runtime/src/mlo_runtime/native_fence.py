"""Fixed host-local native protocol for a separately hash-bound SSM adapter.

stdout contains one receipt, never credentials. This is the target-host half;
remote reachability/SSM identity is still verified by the existing controller.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .lease import PostgresLease
from .native_execution import NativeAdmission, SystemdSparkExecution
from .operations import UnsafeTransition


def protected(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_uid != 0 or path.stat().st_mode & 0o077 or os.geteuid() != 0:
        raise UnsafeTransition("native fencing input must be root-owned 0600")
    return path


def dispatch(binding, lease, barrier, token, operation, *, execution=None, admission=None):
    execution = execution or SystemdSparkExecution(binding)
    registry = Path(binding["registry"])
    if operation == "start":
        current = lease.read()
        if current is None or any(current[k] != token[k] for k in ("owner", "generation", "epoch")):
            raise UnsafeTransition("native group start has no matching authoritative lease")
        drivers = binding.get("continuous_drivers", [])
        if (not isinstance(drivers, list) or len(drivers) != 2
                or {item.get("query") for item in drivers if isinstance(item, dict)}
                != {"weather-stream", "feedback-postgresql"}):
            raise UnsafeTransition("complete native continuous driver set required")
        admission = admission or NativeAdmission(binding, lease, barrier, host=execution.host)
        try:
            for item in drivers:
                admission.admit(current, item["query"], item["specification"], item["sha256"])
        except BaseException:
            # A successful first admission is already a live PostgreSQL writer.
            # Even if the second admission/observation fails, always attempt to
            # stop the whole known epoch group; preserve pending state so an
            # uncertain stop can never become automatic takeover approval.
            try:
                execution.stop_group(current)
            except BaseException:
                pass
            raise UnsafeTransition(
                "native group admission failed; known writers fenced where possible; "
                "uncertain group blocks takeover"
            ) from None
        return {"owner": token["owner"], "generation": token["generation"], "epoch": token["epoch"],
                "operation": "start", "accepted": True, "running": True, "execution_backend": "native-systemd-v1"}
    if operation == "observe" and not registry.exists():
        # No previous lease is the only valid cold-start case. A lost registry
        # with an expired lease must never be interpreted as stopped evidence.
        if lease.read() is not None:
            raise UnsafeTransition("native registry missing for an existing owner")
        if execution.host.command(["systemctl", "list-units", "--all", "--plain", "--no-legend", "mlo-driver-*.service"]).strip():
            raise UnsafeTransition("native cold start found unregistered units")
        proof = execution.cluster_proof(token, True)
        return {"owner": token["owner"], "generation": token["generation"], "epoch": token["epoch"],
                "operation": operation, "accepted": True, "running": False,
                "execution_backend": "native-systemd-v1", "native_units": [],
                "masters": list(execution.masters), "workers": list(execution.workers), "evidence": proof.__dict__}
    return execution.receipt(token, operation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["start", "stop", "observe"], required=True)
    for name in ("owner", "generation", "epoch"):
        parser.add_argument("--" + name, required=True, type=int if name == "epoch" else str)
    args = parser.parse_args()
    try:
        binding = json.loads(protected("/etc/mlo/native-execution.json").read_text())
        if (binding.get("accepted") is not True or binding.get("example_only") is not False
                or binding.get("execution_backend") != "native-systemd-v1"):
            raise UnsafeTransition("native fencing contract is not accepted for real execution")
        if (args.owner, args.generation) != (binding["owner"], binding["generation"]):
            raise UnsafeTransition("native remote target mismatch")
        lease = PostgresLease(protected(binding["lease_dsn_file"]).read_text().strip(), binding["group"])
        from .contracts import ROOT
        sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
        from infra_common.factory import load_guard
        from infra_common.coordination import MaintenanceBarrier
        guard = load_guard(binding["coordination_contract"], binding["coordination_scope"], "native-driver-" + args.owner, "deploy")
        barrier = MaintenanceBarrier(guard.store, guard.scope)
        token = {"owner": args.owner, "generation": args.generation, "epoch": args.epoch, "expires_at": time.time() + 15}
        print(json.dumps(dispatch(binding, lease, barrier, token, args.operation), sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError):
        print("native fencing refused/failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
