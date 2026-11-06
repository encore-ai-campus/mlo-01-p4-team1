"""Regression tests for review C-R1..3 and the additional local safety boundaries."""

import copy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mlo_lifecycle.adapter import Simulator, SQLiteStatefulBackend, database, initialize, stop_stateful
from mlo_lifecycle.cli_io import InputSnapshots, OutputReservation
from mlo_lifecycle.engine import authorize, execute
from mlo_lifecycle.journal import Journal, atomic, sign
from mlo_lifecycle.model import Refused, code_hash, digest, read, validate_bundle
from mlo_lifecycle.planner import plan
from support import authorization, fixture


class SafetyRegressionTests(unittest.TestCase):
    def test_output_parent_replacement_does_not_publish_or_remove_other_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "reports"
            parent.mkdir()
            output = OutputReservation(parent / "result.json")
            output.reserve()
            saved = Path(tmp) / "moved-reports"
            parent.rename(saved)
            parent.symlink_to(saved, target_is_directory=True)
            with self.assertRaises(Refused) as caught:
                output.publish({"unexpected": "overwrite"})
            self.assertEqual(caught.exception.code, "report_write_failure")
            output.close()
            self.assertEqual(read(saved / "result.json")["status"], "reserved")

    def test_protection_is_code_policy_not_untrusted_snapshot(self):
        for mutation in (False, None, "missing"):
            b = fixture()
            item = next(r for r in b["states"]["encore1"]["resources"] if r["address"].startswith("aws_ebs_volume"))
            if mutation == "missing":
                del item["protected"]
            else:
                item["protected"] = mutation
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_bundle(b)
        b = fixture()
        next(r for r in b["states"]["encore1"]["resources"] if r["address"].startswith("aws_instance"))["protected"] = True
        with self.assertRaises(ValueError):
            validate_bundle(b)
        self.assertTrue(validate_bundle(fixture()))

    def test_purge_any_topological_schedule_keeps_backup_access_alive(self):
        b = fixture()
        b["dependencies"] = dict(reversed(list(b["dependencies"].items())))
        p = plan(b, "purge-data")
        backups = {s["id"] for s in p["steps"] if s["action"] == "backup-data"}
        self.assertEqual(len(backups), 20)
        # Enumerate maximal parallel layers, not just the planner's returned order.
        pending = {s["id"]: s for s in reversed(p["steps"])}
        done = set()
        while pending:
            ready = [s for s in pending.values() if set(s["requires"]) <= done]
            self.assertTrue(ready)
            for s in ready:
                if s["action"] == "delete":
                    self.assertTrue(backups <= done)
                    self.assertTrue(backups <= set(s["requires"]))
            done.update(s["id"] for s in ready)
            for s in ready:
                del pending[s["id"]]

    def test_expired_plan_blocks_next_effect(self):
        b = fixture()
        start = time.time()
        p = plan(b, now=start, ttl=1)
        key = b"local-test-authority-000000000000"
        a, e = authorization(p, b, key, now=start)
        with tempfile.TemporaryDirectory() as tmp:
            lab = Path(tmp) / "lab"
            initialize(lab, b)
            with patch("mlo_lifecycle.engine.time.time", side_effect=[start, start + 2]):
                with self.assertRaises(Refused) as caught:
                    execute(p, b, lab, a, e, key)
            self.assertEqual(caught.exception.code, "plan_expired")
            self.assertTrue(all(Simulator(lab, b).assets().values()))
            self.assertFalse((lab / "journal.jsonl").exists())

    def test_code_change_during_effect_preserves_ambiguous_journal(self):
        b = fixture()
        p = plan(b)
        key = b"local-test-authority-000000000000"
        a, e = authorization(p, b, key)
        with tempfile.TemporaryDirectory() as tmp:
            lab = Path(tmp) / "lab"
            initialize(lab, b)
            with patch("mlo_lifecycle.engine.code_hash", side_effect=[p["code_sha256"], "0" * 64]):
                with self.assertRaises(Refused) as caught:
                    execute(p, b, lab, a, e, key)
            self.assertEqual(caught.exception.code, "artifact_drift")
            self.assertEqual(Journal(lab, p["sha256"], key).latest()["freeze"]["phase"], "ambiguous")
            self.assertTrue(all(Simulator(lab, b).assets().values()))

    def test_input_snapshot_whitespace_replacement_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "input.json"
            p.write_text('{}')
            snapshot = InputSnapshots([p])
            p.write_text('{}\n')
            with self.assertRaises(Refused):
                snapshot.check()
            snapshot = InputSnapshots([p])
            other = p.with_name('new.json')
            other.write_bytes(p.read_bytes())
            other.replace(p)
            with self.assertRaises(Refused):
                snapshot.check()
            link = p.with_name('link.json')
            link.symlink_to(p)
            with self.assertRaises(Refused):
                InputSnapshots([link])

    def test_runtime_issuer_instance_service_and_boolean_evidence_refused(self):
        b = fixture()
        p = plan(b)
        key = b"local-test-authority-000000000000"
        a, e = authorization(p, b, key)
        for mutation in ("issuer", "instance", "missing-service", "false-service", "numeric-true", "reviewer"):
            altered = copy.deepcopy(e)
            altered.pop("signature")
            runtime = altered["runtime"]
            if mutation == "issuer":
                runtime["issuer"] = "untrusted"
            elif mutation == "instance":
                runtime["instances"]["control-a"] = "i-wrong"
            elif mutation == "missing-service":
                del runtime["services"]["delta_checkpoint_flushed"]
            elif mutation == "false-service":
                runtime["services"]["spark_executors_absent"] = False
            elif mutation == "numeric-true":
                altered["facts"]["identity_verified"] = 1
            else:
                approval = {k: v for k, v in a.items() if k != "signature"}
                approval["reviewer"] = "untrusted"
                with self.assertRaises(Refused):
                    authorize(p, b, sign(approval, key), e, key, time.time())
                continue
            with self.subTest(mutation=mutation), self.assertRaises(Refused) as caught:
                authorize(p, b, a, sign(altered, key), key, time.time())
            self.assertEqual(caught.exception.code, "authorization_failure")

    def test_hash_change_between_copy_check_restore_and_stop_blocks_database(self):
        for boundary in ("before-restore-copy", "before-stop-copy", "before-stop-archive", "before-stop-source", "before-stop-restored"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                b = fixture()
                lab = Path(tmp) / "lab"
                initialize(lab, b)
                backend = SQLiteStatefulBackend(lab, "postgresql", b["binding"]["generation"])
                original = backend.verify_restore
                def changed(node, copied):
                    if boundary == "before-restore-copy":
                        with Path(copied["location"]).open('ab') as stream:
                            stream.write(b'changed')
                    result = original(node, copied)
                    if boundary == "before-stop-source":
                        with database(backend.source) as db:
                            db.execute("insert into events values(100,'unexpected')")
                    elif boundary.startswith("before-stop"):
                        name = {"before-stop-copy": "independent/backup.db", "before-stop-archive": "backup.db",
                                "before-stop-restored": "restored.db"}[boundary]
                        with (backend.folder / name).open('ab') as stream:
                            stream.write(b'changed')
                    return result
                backend.verify_restore = changed
                with self.assertRaises(RuntimeError):
                    stop_stateful("postgresql", backend, receipt_path=backend.folder / "stop.receipt.json")
                self.assertFalse(read(backend.flags)["stopped"])

    def test_completed_stateful_receipt_without_effect_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = fixture()
            lab = Path(tmp) / "lab"
            initialize(lab, b)
            adapter = Simulator(lab, b)
            for action in ("freeze", "idle"):
                adapter.apply({"id": action, "action": action, "target": None})
            step = {"id": "stateful:mongodb", "action": "stateful", "target": "mongodb"}
            adapter.apply(step)
            with database(lab / "simulator.db") as db:
                db.execute("delete from effects where step=?", (step["id"],))
            self.assertEqual(read(lab / "db-mongodb/stop.receipt.json")["phase"], "complete")
            self.assertEqual(adapter.observe(step)["status"], "unknown")
            with self.assertRaises(Refused):
                adapter.apply(step)

    def test_purge_source_and_copy_drift_invalidate_completion(self):
        for suffix in (".json", ".copy.json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                b = fixture()
                lab = Path(tmp) / "lab"
                initialize(lab, b)
                adapter = Simulator(lab, b)
                for action in ("freeze", "idle"):
                    adapter.apply({"id": action, "action": action, "target": None})
                target = next(k for k in b["binding"]["targets"] if "aws_ebs_volume" in k)
                step = {"id": "backup-data:" + target, "action": "backup-data", "target": target}
                adapter.apply(step)
                path = lab / "data" / (digest(target) + suffix)
                path.write_text('{"changed":true}')
                with self.assertRaises(ValueError):
                    adapter.observe(step)

    def test_journal_checkpoint_rollback_missing_duplicate_and_partial_write(self):
        key = b"local-test-authority-000000000000"
        for mutation in ("rollback", "missing-checkpoint", "duplicate", "partial", "missing-journal"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                journal = Journal(tmp, "plan", key)
                journal.append("freeze", "started")
                old = journal.checkpoint.read_bytes()
                journal.append("freeze", "complete", {"status": "completed"})
                if mutation == "rollback":
                    journal.checkpoint.write_bytes(old)
                elif mutation == "missing-checkpoint":
                    journal.checkpoint.unlink()
                elif mutation == "missing-journal":
                    journal.path.unlink()
                else:
                    with journal.path.open('ab') as stream:
                        stream.write(journal.path.read_bytes().splitlines()[0] + b'\n' if mutation == "duplicate" else b'{"partial":')
                with self.assertRaises((ValueError, OSError)):
                    Journal(tmp, "plan", key)
