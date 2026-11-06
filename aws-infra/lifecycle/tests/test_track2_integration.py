"""Actual C CLI/journal, explicit synthetic Track2 hooks and SQLite backups."""

import copy
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from mlo_lifecycle.journal import Journal, atomic, locked, sign
from mlo_lifecycle.model import BASE, digest, file_hash, read
from mlo_lifecycle.recovery import recovery_hash
from support import cli
from track2_support import prepare_track2


class Track2Integration(unittest.TestCase):
    def test_cli_shutdown_then_isolated_recovery_and_rollback_preserve_source(self):
        for rollback in (False, True):
            with self.subTest(rollback=rollback), tempfile.TemporaryDirectory(prefix="mlo-c-track2-") as tmp:
                bundle, plan, key, args = prepare_track2(tmp, rollback=rollback)
                lab = Path(tmp) / "lab"
                assets = {p.name: file_hash(p) for p in (lab / "track2-assets").iterdir()}
                dbs = {name: file_hash(lab / ("db-" + name) / "source.db") for name in ("postgresql", "mongodb")}
                result = cli("execute", *args)
                self.assertTrue(result["complete"])
                self.assertEqual(result["track2"]["actual_app_hooks"], "not_run")
                recovered = cli("restore-track2", *args)
                self.assertTrue(recovered["complete"])
                self.assertFalse(recovered["writers_started"])
                self.assertFalse(recovered["pitr_verified"])
                alias = read(lab / "track2-recovery/alias.json")
                self.assertEqual(alias["release_id"], "release-one" if rollback else "release-two")
                self.assertNotIn("venue-b", [d["entity_id"] for d in alias["documents"]])
                self.assertEqual(read(lab / "track2-recovery/business-replay.json")["reconciled_event_ids"], ["event-c"])
                logs = read(lab / "track2-recovery/observation-replay.json")
                self.assertEqual(len(logs["events"]), 3)
                self.assertNotIn("PUBLISHED", logs["stage_counts"])
                for path in (lab / "track2-assets").iterdir():
                    self.assertEqual(file_hash(path), assets[path.name])
                for name, sha in dbs.items():
                    self.assertEqual(file_hash(lab / ("db-" + name) / "source.db"), sha)
                before = (lab / "track2-recovery/journal.jsonl").read_bytes()
                cli("restore-track2", *args, expected=2)
                self.assertTrue(cli("resume-track2", *args)["complete"])
                self.assertEqual((lab / "track2-recovery/journal.jsonl").read_bytes(), before)
                self.assertTrue(cli("verify-track2", *args)["complete"])
                alias["documents"] = []
                atomic(lab / "track2-recovery/alias.json", alias)
                report = cli("verify-track2", *args, expected=3)
                self.assertEqual(report["failed"], ["alias"])

    def test_pending_unknown_queue_hook_failure_and_revived_writer_block_backup(self):
        mutations = {
            "outbox": lambda s: s.update(pending_outbox=1),
            "unknown-outbox": lambda s: s.update(pending_outbox=None),
            "queue": lambda s: s.update(queue_observations_known=False),
            "hook": lambda s: s["writers"]["mail-worker"].update(allow_stop=False),
            "pending": lambda s: s["writers"]["outbox-exporter"].update(pending=1),
            "unknown": lambda s: s["writers"]["ads-writer"].update(unknown=1),
            "old-writer": lambda s: s.update(old_writers=["old-owner"]),
            "mixed-epoch": lambda s: s["writers"]["catalog-driver"].update(epoch=7),
            "missing-worker": lambda s: s["writers"].pop("app-synthetic-worker"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="mlo-c-track2-deny-") as tmp:
                bundle, plan, key, args = prepare_track2(tmp)
                lab = Path(tmp) / "lab"
                state = read(lab / "track2-state.json")
                mutate(state)
                atomic(lab / "track2-state.json", state)
                cli("execute", *args, expected=2)
                self.assertFalse((lab / "db-postgresql/backup.db").exists())
                self.assertFalse((lab / "db-mongodb/backup.db").exists())
                cli("restore-track2", *args, expected=2)

    def test_shutdown_lost_response_requires_signed_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-track2-drain-resume-") as tmp:
            bundle, plan, key, args = prepare_track2(tmp)
            step = "track2:drain:search-publisher"
            cli("execute", *args, "--fault-step", step, "--fault", "timeout", expected=2)
            cli("resume", *args, expected=2)
            observed = cli("observe", *args, "--step", step)
            self.assertEqual(observed["status"], "completed")
            now = time.time()
            atomic(Path(tmp) / "rec.json", [sign({"kind": "local-reconciliation-v1", "reviewer": "local-test",
                   "plan_sha256": plan["sha256"], "step": step, "decision": "completed",
                   "observation_sha256": digest(observed), "issued_at": now, "expires_at": now + 120}, key)])
            self.assertTrue(cli("resume", *args, "--reconciliation", Path(tmp) / "rec.json")["complete"])

    def test_each_stateful_hook_rechecks_track2_seal_before_source_barrier(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-track2-hook-boundary-") as tmp:
            bundle, plan, key, args = prepare_track2(tmp)
            lab = Path(tmp) / "lab"
            cli("execute", *args, "--fault-step", "stateful:mongodb", "--fault", "before", expected=2)
            state = read(lab / "track2-state.json")
            state["queue_observations_known"] = False
            atomic(lab / "track2-state.json", state)
            result = subprocess.run([str(BASE / "scripts/stateful-hook.py"), "--operation", "quiesce",
                                    "--node", "mongodb", "--generation", bundle["binding"]["generation"]],
                                    env={**os.environ, "MLO_C_LAB": str(lab)}, capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(read(lab / "db-mongodb/control.json")["writes_blocked"])

    def test_recovery_partial_failure_and_crash_resume_never_duplicate_replay(self):
        for fault in ("before", "timeout", "crash"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory(prefix="mlo-c-track2-recovery-resume-") as tmp:
                bundle, plan, key, args = prepare_track2(tmp)
                cli("execute", *args)
                cli("restore-track2", *args, "--fault-step", "business-replay", "--fault", fault,
                    expected=77 if fault == "crash" else 2)
                cli("resume-track2", *args, expected=2)
                observed = cli("observe-track2", *args, "--step", "business-replay")
                self.assertEqual(observed["status"], "not_started" if fault == "before" else "completed")
                now = time.time()
                atomic(Path(tmp) / "rec.json", [sign({"kind": "local-reconciliation-v1", "reviewer": "local-test",
                       "plan_sha256": recovery_hash(plan, bundle["track2"], False), "step": "business-replay",
                       "decision": "retry" if fault == "before" else "completed",
                       "observation_sha256": digest(observed), "issued_at": now, "expires_at": now + 120}, key)])
                self.assertTrue(cli("resume-track2", *args, "--reconciliation", Path(tmp) / "rec.json")["complete"])
                self.assertEqual(len(read(Path(tmp) / "lab/track2-recovery/business-replay.json")["events"]), 2)

    def test_recovery_requires_new_approval_and_rejects_old_writer_and_asset_drift(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-track2-evidence-") as tmp:
            bundle, plan, key, args = prepare_track2(tmp)
            lab = Path(tmp) / "lab"
            cli("execute", *args)
            approved = read(Path(tmp) / "recovery-approval.json")
            wrong = copy.deepcopy(approved)
            wrong.pop("signature")
            wrong["recovery_sha256"] = "0" * 64
            atomic(Path(tmp) / "recovery-approval.json", sign(wrong, key))
            cli("restore-track2", *args, expected=2)
            atomic(Path(tmp) / "recovery-approval.json", approved)
            state = read(lab / "track2-state.json")
            original = copy.deepcopy(state)
            state["writers"]["catalog-driver"]["stopped"] = False
            atomic(lab / "track2-state.json", state)
            cli("restore-track2", *args, expected=2)
            atomic(lab / "track2-state.json", original)
            path = lab / "track2-assets/checkpoint-venues-catalog.json"
            asset = read(path)
            asset["version_id"] = "changed-version"
            atomic(path, asset)
            cli("restore-track2", *args, expected=2)
            self.assertFalse((lab / "track2-recovery").exists())

    def test_shared_source_lock_and_recovery_journal_tampering_refused(self):
        with tempfile.TemporaryDirectory(prefix="mlo-c-track2-lock-") as tmp:
            bundle, plan, key, args = prepare_track2(tmp)
            lab = Path(tmp) / "lab"
            cli("execute", *args)
            with locked(lab):
                cli("restore-track2", *args, expected=2)
            cli("restore-track2", *args)
            checkpoint = lab / "track2-recovery/checkpoint.json"
            value = read(checkpoint)
            value["head"] = "0" * 64
            atomic(checkpoint, value)
            cli("resume-track2", *args, expected=2)


if __name__ == "__main__":
    unittest.main()
