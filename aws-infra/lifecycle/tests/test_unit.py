"""C-U1..U5: identity, policy, approval, dependency and durable-state refusal."""

import copy
import fcntl
import json
from pathlib import Path
import tempfile
import time
import unittest

from mlo_lifecycle.adapter import Simulator, SQLiteStatefulBackend, database, initialize, stop_stateful
from mlo_lifecycle.engine import authorize, execute, verify
from mlo_lifecycle.journal import Journal, atomic, sign
from mlo_lifecycle.model import Refused, digest, read, validate_bundle
from mlo_lifecycle.planner import order, plan, validate_plan
from support import authorization, fixture


class TargetTests(unittest.TestCase):
    def test_valid_seven_roots_sixteen_nodes(self):
        bundle = fixture()
        resources = validate_bundle(bundle)
        self.assertEqual(len([r for r in resources.values() if r["kind"] == "compute"]), 16)
        self.assertEqual(set(bundle["states"]), {"encore1", "encore2", "encore3", "encore4", "encore5", "bootstrap", "connectivity"})

    def test_account_environment_generation_and_real_inputs_refused(self):
        for key, value in [("project", "other"), ("environment", "prod"), ("generation", "other")]:
            b = fixture()
            b["binding"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_bundle(b)
        for location in ("example_only", "inventory"):
            b = fixture()
            if location == "inventory":
                b[location]["example_only"] = False
            else:
                b[location] = False
            with self.assertRaises(ValueError):
                validate_bundle(b)
        b = fixture()
        b["binding"]["account_ids"]["encore1"] = "999999999999"
        with self.assertRaises(ValueError):
            validate_bundle(b)

    def test_wrong_unknown_external_or_duplicate_target_refused(self):
        for field, value in [("ownership", "unknown"), ("project", "other"), ("account", "encore5"),
                             ("id", "foreign-vpc"), ("address", "aws_vpc.untracked")]:
            b = fixture()
            b["states"]["encore1"]["resources"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_bundle(b)
        for mutation in ("duplicate", "missing", "unbound"):
            b = fixture()
            if mutation == "duplicate":
                b["states"]["encore1"]["resources"].append(b["states"]["encore1"]["resources"][0])
            elif mutation == "missing":
                b["states"]["encore1"]["resources"].pop()
            else:
                b["binding"]["targets"].pop(next(iter(b["binding"]["targets"])))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_bundle(b)

    def test_backend_lineage_serial_and_root_refused(self):
        for field, value in [("serial", 8), ("lineage", "other"), ("backend", {"kind": "s3", "key": "dev/encore1/terraform.tfstate"})]:
            b = fixture()
            b["states"]["encore1"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_bundle(b)
        b = fixture()
        del b["states"]["bootstrap"]
        with self.assertRaises(ValueError):
            validate_bundle(b)

    def test_json_duplicate_and_nonfinite_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    read(path)


class PlanTests(unittest.TestCase):
    def test_default_retain_preserves_data_backend_and_network_dependencies(self):
        p = plan(fixture())
        self.assertFalse(p["ready_for_aws"])
        self.assertTrue(p["cost_may_remain"])
        self.assertTrue(all(r["kind"] not in {"data", "backend", "network"} for k, r in p["targets"].items() if k in p["delete"]))
        self.assertEqual(len([k for k in p["retain"] if p["targets"][k]["kind"] == "data"]), 20)

    def test_cycle_missing_dependency_and_early_connectivity_mutations_refused(self):
        for kind in ("cycle", "missing"):
            b = fixture()
            a, c = list(b["dependencies"])[:2]
            b["dependencies"][a] = [c] if kind == "cycle" else ["absent"]
            b["dependencies"][c] = [a]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                plan(b)
        b = fixture()
        p = plan(b)
        for s in p["steps"]:
            if s["action"] == "delete" and p["targets"][s["target"]]["kind"] == "connectivity":
                s["requires"] = []
                break
        with self.assertRaises(ValueError):
            validate_plan(p, b)

    def test_plan_tampering_expiry_unmigrated_data_and_code_hash_refused(self):
        b = fixture()
        for key, value in [("code_sha256", "0" * 64), ("sha256", "0" * 64), ("retain", []), ("adapter", "real")]:
            p = plan(b)
            p[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan(p, b)
        p = plan(b, now=time.time() - 4000)
        with self.assertRaises(ValueError):
            validate_plan(p, b)
        b["binding"]["untracked_data"] = ["root EBS spool not migrated"]
        p = plan(b)
        self.assertTrue(p["blockers"])
        with self.assertRaises(ValueError):
            validate_plan(p, b)

    def test_backend_is_preserved_in_purge_and_connection_follows_compute(self):
        p = plan(fixture(), "purge-data")
        self.assertEqual(p["retain"], ["bootstrap:aws_s3_bucket.state"])
        positions = {s["id"]: n for n, s in enumerate(p["steps"])}
        compute = [positions["delete:" + k] for k in p["delete"] if p["targets"][k]["kind"] == "compute"]
        for k in p["delete"]:
            if p["targets"][k]["kind"] in {"connectivity", "access", "network"}:
                self.assertGreater(positions["delete:" + k], max(compute))


class ApprovalAndJournalTests(unittest.TestCase):
    def setUp(self):
        self.bundle = fixture()
        self.plan = plan(self.bundle, "purge-data")
        self.key = b"test-local-authority-key-00000000"
        self.now = time.time()
        self.approval, self.evidence = authorization(self.plan, self.bundle, self.key, self.now)

    def test_authorization_accepts_exact_scope(self):
        authorize(self.plan, self.bundle, self.approval, self.evidence, self.key, self.now)

    def test_missing_purge_protection_or_approval_scope_refused(self):
        for field, value in [("purge_data", []), ("protection_release", []), ("delete", []),
                             ("plan_sha256", "f" * 64), ("reviewer", "")]:
            a = dict(self.approval)
            a.pop("signature")
            a[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                authorize(self.plan, self.bundle, sign(a, self.key), self.evidence, self.key, self.now)

    def test_forged_stale_missing_future_and_false_evidence_refused(self):
        for record_name in ("approval", "evidence"):
            for mutation in ("forged", "expired", "future", "missing", "false"):
                a, e = copy.deepcopy(self.approval), copy.deepcopy(self.evidence)
                item = a if record_name == "approval" else e
                if mutation == "forged":
                    item["signature"] = "0" * 64
                else:
                    item.pop("signature")
                    if mutation == "expired":
                        item["issued_at"], item["expires_at"] = self.now - 500, self.now - 200
                    elif mutation == "future":
                        item["issued_at"], item["expires_at"] = self.now + 10, self.now + 50
                    elif mutation == "missing":
                        item.pop("scope")
                    elif record_name == "evidence":
                        item["facts"]["identity_verified"] = False
                    else:
                        item["scope"]["generation"] = "other"
                    item.update(sign(item, self.key))
                with self.subTest(record=record_name, mutation=mutation), self.assertRaises(ValueError):
                    authorize(self.plan, self.bundle, a, e, self.key, self.now)

    def test_journal_tamper_truncation_and_other_plan_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Journal(tmp, self.plan["sha256"], self.key)
            j.append("freeze", "started")
            j.append("freeze", "complete", {"status": "completed"})
            self.assertEqual(len(Journal(tmp, self.plan["sha256"], self.key).events), 2)
            with self.assertRaises(ValueError):
                Journal(tmp, "other-plan", self.key)
            lines = j.path.read_text().splitlines()
            j.path.write_text(lines[0] + "\n")
            with self.assertRaises(ValueError):
                Journal(tmp, self.plan["sha256"], self.key)
            j.path.write_text("\n".join(lines).replace('"started"', '"ambiguous"') + "\n")
            with self.assertRaises(ValueError):
                Journal(tmp, self.plan["sha256"], self.key)

    def test_concurrency_creation_deployment_and_new_plan_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab = Path(tmp) / "lab"
            initialize(lab, self.bundle)
            with (lab / "execution.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ValueError):
                    execute(self.plan, self.bundle, lab, self.approval, self.evidence, self.key)
            for key in ("creation", "deployment"):
                atomic(lab / "activity.json", {"creation": key == "creation", "deployment": key == "deployment"})
                with self.assertRaises(ValueError):
                    execute(self.plan, self.bundle, lab, self.approval, self.evidence, self.key)
            atomic(lab / "activity.json", {"creation": False, "deployment": False})
            Simulator(lab, self.bundle).preflight("another-plan", bind=True)
            with self.assertRaises(ValueError):
                execute(self.plan, self.bundle, lab, self.approval, self.evidence, self.key)


class AdapterInvariantTests(unittest.TestCase):
    def test_absent_effect_is_not_proof_of_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = fixture()
            lab = Path(tmp) / "lab"
            initialize(lab, bundle)
            adapter = Simulator(lab, bundle)
            compute = next(k for k in bundle["binding"]["targets"] if ":aws_instance." in k)
            step = {"id": "delete:" + compute, "action": "delete", "target": compute}
            with database(lab / "simulator.db") as connection:
                connection.execute("update assets set present=0 where target=?", (compute,))
            self.assertEqual(adapter.observe(step), {"ready_for_aws": False, "status": "unknown"})
            with self.assertRaises(ValueError):
                adapter.apply(step)

    def test_copy_corruption_and_restore_failure_never_stop_database(self):
        for kind in ("hash", "restore"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                lab = Path(tmp) / "lab"
                bundle = fixture()
                initialize(lab, bundle)
                backend = SQLiteStatefulBackend(lab, "postgresql", bundle["binding"]["generation"])
                if kind == "hash":
                    original = backend.verify_copy
                    def corrupt(node, copied):
                        Path(copied["location"]).write_bytes(b"corrupt")
                        return original(node, copied)
                    backend.verify_copy = corrupt
                else:
                    (backend.folder / "restored.db").write_bytes(b"do-not-replace")
                with self.assertRaises(RuntimeError):
                    stop_stateful("postgresql", backend, receipt_path=backend.folder / "negative-stop.json")
                self.assertFalse(read(backend.flags)["stopped"])
                self.assertEqual(read(backend.folder / "negative-stop.json")["phase"], "blocked")

    def test_backup_source_drift_and_retained_missing_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = fixture()
            p = plan(bundle)
            key = b"local-test-authority-000000000000"
            approval, evidence = authorization(p, bundle, key)
            lab = Path(tmp) / "lab"
            initialize(lab, bundle)
            execute(p, bundle, lab, approval, evidence, key)
            with database(lab / "db-postgresql/source.db") as connection:
                connection.execute("insert into events values(100,'unexpected post-backup write')")
            retained = p["retain"][0]
            with database(lab / "simulator.db") as connection:
                connection.execute("update assets set present=0 where target=?", (retained,))
            report = verify(p, bundle, lab, key)
            self.assertFalse(report["complete"])
            self.assertIn("stateful:postgresql", report["failed"])
            self.assertIn(retained, report["retained_missing"])
            self.assertEqual(report["target_status"][retained], "missing_retained")


if __name__ == "__main__":
    unittest.main()
