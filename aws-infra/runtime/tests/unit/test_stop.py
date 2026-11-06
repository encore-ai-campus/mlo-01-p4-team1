import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mlo_runtime.operations import UnsafeTransition, local_lock, rollout, stop_stateful


class Stateful:
    def __init__(self):
        self.events = []
        self.failed = None

    def event(self, name):
        self.events.append(name)
        if self.failed == name:
            raise RuntimeError(name)

    def quiesce(self, node):
        self.event("quiesce")
        return {"node": node, "barrier_id": "frozen-1", "writes_blocked": True}

    def backup(self, node):
        self.event("backup")
        return {
            "verified": True,
            "sha256": "a" * 64,
            "location": "local:/backup",
            "barrier_id": "frozen-1",
        }

    def copy_backup(self, node, archive):
        self.event("copy")
        return {"sha256": "a" * 64, "location": "independent:/backup"}

    def verify_copy(self, node, archive):
        self.event("verify-copy")
        return {"verified": True, "sha256": "a" * 64}

    def verify_restore(self, node, archive):
        self.event("verify-restore")
        return {"verified": True, "sha256": "a" * 64}

    def stop(self, node):
        self.event("stop")

    def observe_stopped(self, node):
        self.event("observe-stopped")
        return True


class StopTests(unittest.TestCase):
    def test_every_backup_stage_failure_blocks_stop(self):
        for failed in ("quiesce", "backup", "copy", "verify-copy", "verify-restore"):
            backend = Stateful()
            backend.failed = failed
            with self.subTest(failed=failed), self.assertRaises(UnsafeTransition):
                stop_stateful("postgresql", backend)
            self.assertNotIn("stop", backend.events)

    def test_stop_records_exact_order_and_bound_receipt(self):
        backend = Stateful()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stop.json"
            result = stop_stateful("postgresql", backend, receipt_path=path)
            self.assertEqual(
                backend.events,
                [
                    "quiesce",
                    "backup",
                    "copy",
                    "verify-copy",
                    "verify-restore",
                    "stop",
                    "observe-stopped",
                ],
            )
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(result["phase"], "complete")

    def test_ambiguous_stop_requires_reconciliation(self):
        backend = Stateful()
        backend.failed = "stop"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "stop.json"
            with self.assertRaises(UnsafeTransition):
                stop_stateful("postgresql", backend, receipt_path=path)
            backend.failed = None
            with self.assertRaises(UnsafeTransition):
                stop_stateful("postgresql", backend, receipt_path=path)
            self.assertEqual(json.loads(path.read_text())["phase"], "stop_ambiguous")

    def test_rollout_lifetime_lock_and_crash_receipt_block_retry(self):
        class Crashing:
            def snapshot(self, node):
                return "old"

            def check_migration(self, *args):
                pass

            def drain(self, node):
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rollout.json"
            with local_lock(str(path) + ".lock"), self.assertRaises(UnsafeTransition):
                rollout(["a", "b"], "new", Crashing(), receipt_path=path)
            with self.assertRaises(KeyboardInterrupt):
                rollout(["a", "b"], "new", Crashing(), receipt_path=path)
            self.assertEqual(json.loads(path.read_text())["phase"], "replacing")
            with self.assertRaises(UnsafeTransition):
                rollout(["a", "b"], "new", Crashing(), receipt_path=path)


if __name__ == "__main__":
    unittest.main()
