from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest

from mlo_runtime.operations import (
    ActiveGroup,
    FenceEvidence,
    UnsafeTransition,
    atomic_json,
)
from mlo_runtime.supervisor import Supervisor
from mlo_runtime.lease import valid_token


class Lease:
    def __init__(self):
        self.token = None
        self.failed = False

    def read(self):
        if self.failed:
            raise ConnectionError("partition")
        return self.token

    def cas(self, old, new):
        if self.failed:
            raise ConnectionError("partition")
        if old != self.token:
            return False
        self.token = new
        return True


class Execution:
    def __init__(self):
        self.running = False
        self.starts, self.stops = 0, 0
        self.fail_stop = False

    def ensure_started(self, token):
        if not self.running:
            self.starts += 1
            self.running = True

    def stop_group(self, token):
        self.stops += 1
        if self.fail_stop:
            raise UnsafeTransition("old host unavailable")
        self.running = False

    def observe(self, token):
        return FenceEvidence(
            token["owner"],
            token["generation"],
            token["epoch"],
            time.time(),
            not self.running,
            not self.running,
            not self.running,
            not self.running,
            True,
            True,
        )


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.lease, self.execution = Lease(), Execution()
        self.group = ActiveGroup(
            self.path, "g1", "control-a", self.lease, self.execution
        )

    def test_restarts_same_group_without_duplicate_submission(self):
        first = Supervisor(self.group)
        first.step()
        Supervisor(self.group).step()
        self.assertEqual(self.execution.starts, 1)
        self.assertEqual(self.lease.token["epoch"], 1)

    def test_backend_partition_self_fences_and_preserves_old_owner(self):
        worker = Supervisor(self.group)
        worker.step()
        self.lease.failed = True
        with self.assertRaises(UnsafeTransition):
            worker.step()
        self.assertFalse(self.execution.running)
        self.assertEqual(json.loads(self.path.read_text())["phase"], "fenced")

    def test_expired_restart_does_not_resurrect_group(self):
        worker = Supervisor(self.group)
        worker.step()
        token = {**self.lease.token, "expires_at": time.time() - 1}
        self.lease.token = token
        atomic_json(self.path, {"phase": "running", "lease": token})
        with self.assertRaises(UnsafeTransition):
            Supervisor(self.group).step()
        self.assertFalse(self.execution.running)

    def test_remote_stop_failure_prevents_takeover_and_preserves_lease(self):
        old = {
            "owner": "control-a",
            "generation": "g1",
            "epoch": 4,
            "expires_at": time.time() - 1,
        }
        self.lease.token, self.execution.running, self.execution.fail_stop = (
            old,
            True,
            True,
        )
        other = ActiveGroup(self.path, "g1", "control-b", self.lease, self.execution)
        with self.assertRaises(UnsafeTransition):
            Supervisor(other).step()
        self.assertEqual(self.lease.token, old)
        self.assertEqual(self.execution.starts, 0)

    def test_generation_mismatch_does_not_stop_other_group(self):
        old = {
            "owner": "control-a",
            "generation": "old-generation",
            "epoch": 4,
            "expires_at": time.time() - 1,
        }
        self.lease.token = old
        with self.assertRaises(UnsafeTransition):
            self.group.acquire()
        self.assertEqual(self.execution.stops, 0)

    def test_token_rejects_boolean_epoch_and_nonfinite_expiry(self):
        for epoch, expiry in ((True, 100), (1, float("nan")), (1, True)):
            with self.assertRaises(UnsafeTransition):
                valid_token(
                    {
                        "owner": "a",
                        "generation": "g",
                        "epoch": epoch,
                        "expires_at": expiry,
                    }
                )


if __name__ == "__main__":
    unittest.main()
