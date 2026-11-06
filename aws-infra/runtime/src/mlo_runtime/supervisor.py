"""Periodic active-group supervisor with durable crash recovery and self-fencing."""

import argparse
import json
from pathlib import Path
import signal
import threading

from .operations import ActiveGroup, UnsafeTransition, atomic_json, local_lock


class Supervisor:
    def __init__(self, group, interval=3, maintenance_barrier=None):
        if not 0 < interval <= 5:
            raise UnsafeTransition(
                "renewal interval must be <=5 seconds for 15-second lease"
            )
        self.group, self.interval, self.token = group, interval, None
        self.maintenance_barrier = maintenance_barrier

    def step(self):
        group = self.group
        if self.maintenance_barrier is not None:
            try:
                self.maintenance_barrier.check()
            except Exception as exc:
                if self.token is None and group.state.exists():
                    saved = json.loads(group.state.read_text())
                    if saved.get("phase") in {
                        "owned",
                        "starting",
                        "running",
                        "fencing",
                    }:
                        self.token = saved["lease"]
                self.shutdown()
                raise UnsafeTransition(
                    "maintenance inhibits workload admission; local group fenced"
                ) from exc
        if self.token is None and group.state.exists():
            saved = json.loads(group.state.read_text())
            if saved.get("phase") == "fencing":
                group._fence(saved["lease"])
            elif saved.get("phase") in {"owned", "starting", "running"}:
                token = saved["lease"]
                if (
                    token["owner"] != group.owner
                    or token["generation"] != group.generation
                ):
                    raise UnsafeTransition("restart state belongs to another identity")
                # Tick fences rather than resurrecting an expired/replaced owner.
                self.token = group.tick(token)
        if self.token is None:
            self.token = group.acquire()
        else:
            self.token = group.tick(self.token)
        saved = json.loads(group.state.read_text())
        atomic_json(group.state, {**saved, "phase": "starting", "lease": self.token})
        try:
            # Adapter must be idempotent for the exact token: never a second submit.
            group.execution.ensure_started(self.token)
        except Exception:
            group._fence(self.token)
            self.token = None
            raise
        atomic_json(group.state, {**saved, "phase": "running", "lease": self.token})
        return self.token

    def shutdown(self):
        if self.token:
            self.group._fence(self.token)
            self.token = None

    def health(self):
        group = self.group
        token = group.lease.read()
        if (
            not token
            or token["generation"] != group.generation
            or token["expires_at"] <= group.now()
        ):
            raise UnsafeTransition("healthy live lease is unconfirmed")
        if token["owner"] == group.owner:
            if not group.state.exists():
                raise UnsafeTransition("owned lease has no local durable state")
            saved = json.loads(group.state.read_text())
            if saved.get("phase") != "running" or saved.get("lease") != token:
                raise UnsafeTransition("local running state differs from active lease")
            return group.execution.check_host(token, running=True)
        # Standby must have no local workloads. Whole-cluster idle is not required
        # while the other accepted owner is running its group.
        probe = {**token, "owner": group.owner}
        return group.execution.check_host(probe, running=False)

    def run(self, stopped=None):
        stopped = stopped or threading.Event()
        # This lock covers the daemon lifetime, not just an individual renewal.
        with local_lock(str(self.group.state) + ".supervisor.lock"):
            try:
                while not stopped.is_set():
                    try:
                        self.step()
                    except UnsafeTransition:
                        if self.token is not None:
                            # An owned group failing renewal must not be retried
                            # as standby until the fencing attempt has completed.
                            raise
                        # A live owner or uncertain remote fence keeps this host
                        # passive. No workload starts while waiting for evidence.
                        pass
                    stopped.wait(self.interval)
            finally:
                self.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding", type=Path, default=Path("/etc/mlo/supervisor.json")
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.binding.is_symlink() or args.binding.stat().st_mode & 0o077:
        raise UnsafeTransition("supervisor binding must be protected 0600")
    binding = json.loads(args.binding.read_text())
    if binding.get("ready") is not True or binding.get("example_only") is not False:
        raise UnsafeTransition("supervisor inputs are not accepted for execution")
    from .lease import PostgresLease
    from .execution import FixedCommandExecution

    secret = Path(binding["lease_dsn_file"])
    if secret.is_symlink() or secret.stat().st_mode & 0o077:
        raise UnsafeTransition("lease credential must be protected 0600")
    lease = PostgresLease(secret.read_text().strip(), binding["group"])
    execution = FixedCommandExecution(binding["execution"])
    group = ActiveGroup(
        binding["state"], binding["generation"], binding["owner"], lease, execution
    )
    if args.check:
        print(json.dumps(Supervisor(group).health(), sort_keys=True))
        return
    from .contracts import ROOT
    import sys

    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from infra_common.factory import load_guard
    from infra_common.coordination import MaintenanceBarrier

    if not binding.get("coordination_contract") or not binding.get(
        "coordination_scope"
    ):
        raise UnsafeTransition("accepted maintenance barrier required")
    common = load_guard(
        binding["coordination_contract"],
        binding["coordination_scope"],
        "supervisor-" + binding["owner"],
        "deploy",
    )
    barrier = MaintenanceBarrier(common.store, common.scope)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    Supervisor(group, maintenance_barrier=barrier).run(stopped)


if __name__ == "__main__":
    main()
