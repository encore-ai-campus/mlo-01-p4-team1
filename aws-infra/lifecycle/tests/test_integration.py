"""C-I1/I3/I4/I5/I6: actual CLI/B hook/database/journal/failure/resume."""

import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from mlo_lifecycle.adapter import Simulator, database
from mlo_lifecycle.journal import Journal, atomic, sign
from mlo_lifecycle.model import BASE, digest, read
from support import cli, prepare


class LifecycleIntegration(unittest.TestCase):
    def test_cli_inspect_plan_execute_database_backup_restore_and_verify(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-retain-") as tmp:
            bundle, plan, key, args = prepare(tmp)
            inspected = cli("inspect", "--bundle", Path(tmp) / "bundle.json")
            self.assertEqual(set(inspected["targets"]), set(plan["targets"]))
            result = cli("execute", *args)
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["retained"]), 26)
            self.assertFalse(result["all_resources_deleted"])
            self.assertTrue(result["cost_may_remain"])
            for node in ("postgresql", "mongodb"):
                folder = Path(tmp) / "lab" / ("db-" + node)
                receipt = read(folder / "stop.receipt.json")
                self.assertEqual(receipt["phase"], "complete")
                self.assertNotEqual(receipt["backup"]["location"], receipt["independent_copy"]["location"])
                self.assertTrue(receipt["restore_verification"]["verified"])
                with database(folder / "restored.db") as connection:
                    self.assertEqual(connection.execute("select count(*) from events").fetchone(), (8,))
                self.assertTrue((folder / "source.db").exists())
            self.assertTrue(cli("verify", *args)["complete"])
            before = (Path(tmp) / "lab/journal.jsonl").read_bytes()
            cli("execute", *args, expected=2)
            self.assertTrue(cli("resume", *args)["complete"])
            self.assertEqual((Path(tmp) / "lab/journal.jsonl").read_bytes(), before)

    def test_purge_explicit_approval_backup_each_data_target_backend_remains(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-purge-") as tmp:
            bundle, plan, key, args = prepare(tmp, "purge-data")
            approval = read(Path(tmp) / "approval.json")
            original = copy.deepcopy(approval)
            approval.pop("signature")
            approval["purge_data"] = []
            atomic(Path(tmp) / "approval.json", sign(approval, key))
            cli("execute", *args, expected=2)
            self.assertFalse((Path(tmp) / "lab/journal.jsonl").exists())
            atomic(Path(tmp) / "approval.json", original)
            result = cli("execute", *args)
            self.assertTrue(result["complete"])
            self.assertEqual(result["retained"], ["bootstrap:aws_s3_bucket.state"])
            self.assertEqual(len(list((Path(tmp) / "lab/data").glob("*.copy.json"))), 20)
            self.assertTrue(result["cost_may_remain"])

    def test_failure_and_reconciliation_each_phase_no_duplicate_effect(self):
        # Each distinct mutation boundary gets an actual separate C process crash.
        for phase in ("freeze", "idle", "stateful:mongodb", "stateful:postgresql", "delete", "backup-data"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(prefix="mlo-c-crash-") as tmp:
                bundle, plan, key, args = prepare(tmp, "purge-data" if phase == "backup-data" else "retain-data")
                step = (next(s for s in plan["steps"] if s["action"] == phase) if phase in {"delete", "backup-data"}
                        else next(s for s in plan["steps"] if s["id"] == phase))
                cli("execute", *args, "--fault-step", step["id"], "--fault", "crash", expected=77)
                journal = Journal(Path(tmp) / "lab", plan["sha256"], key)
                self.assertEqual(journal.latest()[step["id"]]["phase"], "started")
                report = cli("verify", *args, expected=3)
                self.assertIn(step["id"], report["unknown"])
                before = (Path(tmp) / "lab/journal.jsonl").read_bytes()
                cli("resume", *args, expected=2)
                self.assertEqual((Path(tmp) / "lab/journal.jsonl").read_bytes(), before)
                observed = cli("observe", *args, "--step", step["id"])
                self.assertEqual(observed["status"], "completed")
                reconciliation = sign({"kind": "local-reconciliation-v1", "reviewer": "local-test",
                                       "plan_sha256": plan["sha256"], "step": step["id"], "decision": "completed",
                                       "observation_sha256": digest(observed), "issued_at": time.time(),
                                       "expires_at": time.time() + 180}, key)
                atomic(Path(tmp) / "reconciliation.json", [reconciliation])
                result = cli("resume", *args, "--reconciliation", Path(tmp) / "reconciliation.json")
                self.assertTrue(result["complete"])
                with database(Path(tmp) / "lab/simulator.db") as connection:
                    self.assertEqual(connection.execute("select count(*) from effects").fetchone()[0], len(plan["steps"]))

    def test_lost_reply_before_effect_and_safe_retry(self):
        for fault in ("before", "timeout"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix="mlo-c-timeout-") as tmp:
                bundle, plan, key, args = prepare(tmp)
                cli("execute", *args, "--fault-step", "freeze", "--fault", fault, expected=2)
                observed = cli("observe", *args, "--step", "freeze")
                decision = "retry" if fault == "before" else "completed"
                rec = {"kind": "local-reconciliation-v1", "reviewer": "local-test", "plan_sha256": plan["sha256"],
                       "step": "freeze", "decision": decision, "observation_sha256": digest(observed),
                       "issued_at": time.time(), "expires_at": time.time() + 120}
                bad = {**rec, "decision": "completed" if decision == "retry" else "retry"}
                atomic(Path(tmp) / "bad.json", [sign(bad, key)])
                cli("resume", *args, "--reconciliation", Path(tmp) / "bad.json", expected=2)
                atomic(Path(tmp) / "reconciliation.json", [sign(rec, key)])
                self.assertTrue(cli("resume", *args, "--reconciliation", Path(tmp) / "reconciliation.json")["complete"])

    def test_actual_sigkill_preserves_pending_record_and_requires_observation(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-sigkill-") as tmp:
            bundle, plan, key, args = prepare(tmp)
            # Pause the actual CLI just after its durable 'started' append, then send SIGKILL.
            source = """
import sys,time
from mlo_lifecycle.journal import Journal
original=Journal.append
def append(self,step,phase,observation=None):
    original(self,step,phase,observation)
    if phase=='started': time.sleep(30)
Journal.append=append
from mlo_lifecycle.__main__ import main
sys.argv=['lifecycle','execute',*sys.argv[1:]]
raise SystemExit(main())
"""
            env = {**os.environ, "PYTHONPATH": str(BASE / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
            process = subprocess.Popen([sys.executable, "-c", source, *map(str, args)], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 8
                while not (Path(tmp) / "lab/journal.jsonl").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((Path(tmp) / "lab/journal.jsonl").exists())
                process.kill()
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertEqual(Journal(Path(tmp) / "lab", plan["sha256"], key).latest()["freeze"]["phase"], "started")
                cli("resume", *args, expected=2)
                self.assertEqual(cli("observe", *args, "--step", "freeze"), {"ready_for_aws": False, "status": "not_started"})
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_partial_stateful_hook_failure_does_not_stop_or_delete(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-backup-fail-") as tmp:
            bundle, plan, key, args = prepare(tmp)
            # A real malformed archive causes B's backup boundary to fail before stop.
            (Path(tmp) / "lab/db-mongodb/backup.db").write_bytes(b"existing-unreviewed-backup")
            cli("execute", *args, expected=2)
            control = read(Path(tmp) / "lab/db-mongodb/control.json")
            self.assertFalse(control["stopped"])
            report = cli("verify", *args, expected=3)
            self.assertFalse(report["deleted"])
            self.assertIn("stateful:mongodb", report["unknown"])
            cli("resume", *args, expected=2)

    def test_service_restart_plan_replacement_and_observed_drift_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-drift-") as tmp:
            bundle, plan, key, args = prepare(tmp)
            cli("execute", *args)
            with database(Path(tmp) / "lab/simulator.db") as connection:
                connection.execute("update runtime set value=1 where key='restarts'")
            cli("resume", *args, expected=2)
            report = cli("verify", *args, expected=3)
            self.assertTrue(report["failed"])
            changed = copy.deepcopy(plan)
            changed["steps"] = []
            atomic(Path(tmp) / "plan.json", changed)
            cli("verify", *args, expected=2)

    def test_cli_never_allows_real_adapter_network_or_unexpected_command(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-guard-") as tmp:
            bundle, plan, key, args = prepare(tmp)
            cli("execute", *args, "--adapter", "real", expected=2)
        env = {**os.environ, "PYTHONPATH": str(BASE / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for action in ("import socket;socket.socket()", "import subprocess;subprocess.run(['aws','sts','get-caller-identity'])",
                       "import subprocess;subprocess.run(['terraform','destroy'])", "import os;os.system('true')"):
            result = subprocess.run([sys.executable, "-c", "from mlo_lifecycle.__main__ import guard;guard();" + action],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refused", result.stderr)


if __name__ == "__main__":
    unittest.main()
