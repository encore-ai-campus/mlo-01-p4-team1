"""C->B real PostgreSQL/Mongo dump/independent-version/restore/stop, local only.

All-service evidence is explicitly a signed local-lab fixture. This does not
represent real Spark/systemd/SSM observations or S3 failure-domain acceptance.
"""

import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "aws-infra/contracts"),
    str(ROOT / "aws-infra/runtime/src"),
    str(ROOT / "aws-infra/lifecycle/src"),
    str(ROOT / "aws-infra/lifecycle/tests"),
]
from infra_common.coordination import OperationLease, S3CAS, canonical, scope_key
from infra_common.evidence import SERVICES, validate_shutdown
from local_s3 import LocalS3
from mlo_runtime.database_stop import ContainerStatefulBackend, VersionedBackups
from mlo_runtime.lab import Lab
from mlo_runtime.operations import stop_stateful
from mlo_lifecycle.runtime import stop_engine_backends
from support import fixture


def evidence(inventory, guard, key):
    token = guard.check()
    now = time.time()
    body = {
        "schema_version": "runtime-shutdown-evidence-v1",
        "source": "local-lab",
        "operation_id": token["operation_id"],
        "epoch": token["epoch"],
        "generation": inventory["generation"],
        "nodes": {
            n["name"]: {
                "instance_id": n["instance_id"],
                "generation": inventory["generation"],
            }
            for n in inventory["nodes"]
        },
        "services": {k: True for k in SERVICES},
        "issuer": "local-fixture-observer",
        "observed_at": now,
        "expires_at": now + 30,
    }
    return {
        **body,
        "signature": hmac.new(key, canonical(body), hashlib.sha256).hexdigest(),
    }


class EngineShutdownIntegration(unittest.TestCase):
    def setUp(self):
        self.s3 = LocalS3().__enter__()
        self.client = self.s3.client()
        self.tmp = tempfile.TemporaryDirectory(prefix="mlo-common-engines-")
        self.folder = Path(self.tmp.name)
        self.bundle = fixture()
        self.inventory = self.bundle["inventory"]
        b = self.bundle["binding"]
        self.scope = {
            "project": "mlo-p4",
            "environment": "dev",
            "generation": b["generation"],
            "account_ids": b["account_ids"],
            "state_keys": {
                r: v["backend"]["key"] for r, v in b["state_bindings"].items()
            },
        }
        self.store = S3CAS(
            self.client, "lab-state", "operations/" + scope_key(self.scope) + ".json"
        )
        OperationLease(self.store, self.scope, "bootstrap", "create").initialize()
        self.labs = []
        self.key = b"local-observer-authority-000000000000"

    def tearDown(self):
        for lab in reversed(self.labs):
            lab.close()
        self.client.close()
        self.s3.__exit__()
        self.tmp.cleanup()

    def database(self, node, side):
        lab = Lab("common-" + node + "-" + side)
        self.labs.append(lab)
        service = {"image": lab.image(node), "mem_limit": "512m", "cpus": 1}
        if node == "postgresql":
            service.update(
                environment={
                    "POSTGRES_PASSWORD": "disposable-local-only",
                    "POSTGRES_DB": "mlo_lifecycle",
                },
                volumes=["data:/var/lib/postgresql/data"],
            )
        else:
            service.update(
                command=["mongod", "--bind_ip_all"], volumes=["data:/data/db"]
            )
        lab.configure(service)
        lab.up()
        if node == "postgresql":
            lab.ready(["pg_isready", "-U", "postgres", "-d", "mlo_lifecycle"])
            if side == "source":
                lab.exec(
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "mlo_lifecycle",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "CREATE ROLE app_writer LOGIN; CREATE TABLE events(id serial primary key,payload text not null unique); INSERT INTO events(payload) VALUES('preserve-a'),('preserve-b'); GRANT INSERT,SELECT ON events TO app_writer;",
                )
        else:
            lab.ready(
                [
                    "mongosh",
                    "--quiet",
                    "--eval",
                    "quit(db.adminCommand({ping:1}).ok===1?0:1)",
                ]
            )
            if side == "source":
                lab.exec(
                    "mongosh",
                    "--quiet",
                    "--eval",
                    'const target=db.getSiblingDB("mlo_lifecycle"); target.events.insertMany([{payload:"preserve-a"},{payload:"preserve-b"}]);target.events.createIndex({payload:1},{unique:true});',
                )
        return lab.compose("ps", "-q").stdout.decode().strip()

    def backend(self, node, guard):
        source = self.database(node, "source")
        target = self.database(node, "restore")
        binding = {
            "lab": True,
            "node": node,
            "generation": self.inventory["generation"],
            "database": "mlo_lifecycle",
            "source_id": source,
            "restore_id": target,
            "admin_user": "postgres",
            "writer_roles": ["app_writer"],
        }
        store = VersionedBackups(
            self.client,
            "lab-backups",
            "backups/dev/" + self.inventory["generation"] + "/" + node,
        )
        return ContainerStatefulBackend(
            binding, self.folder / node, store, operation_guard=guard
        )

    def test_C_stops_both_real_engines_only_after_independent_version_restore(self):
        guard = OperationLease(self.store, self.scope, "C", "destroy")
        # Start containers first, then acquire the maintenance operation/evidence.
        backends = {
            node: self.backend(node, None) for node in ["postgresql", "mongodb"]
        }
        with guard.hold():
            for backend in backends.values():
                backend.guard = guard
            result = stop_engine_backends(
                self.inventory,
                backends,
                guard,
                lambda: evidence(self.inventory, guard, self.key),
                {"local-fixture-observer": self.key},
                self.folder / "receipts",
            )
            self.assertTrue(result["complete"])
            self.assertEqual(set(result["completed"]), {"postgresql", "mongodb"})
            for node, backend in backends.items():
                self.assertTrue(backend.observe_stopped(node))
                receipt = json.loads(
                    (self.folder / "receipts" / (node + ".receipt.json")).read_text()
                )
                self.assertEqual(receipt["phase"], "complete")
                self.assertTrue(receipt["independent_copy"]["version_id"])
                self.assertTrue(receipt["restore_verification"]["verified"])
        self.assertEqual(self.store.read()[0]["state"], "complete")

    def test_changed_archive_blocks_each_real_database_before_stop(self):
        for node in ["postgresql", "mongodb"]:
            # Distinct CAS key per test target, not another lock for the same production scope.
            guard = OperationLease(self.store, self.scope, "C-negative", "destroy")
            backend = self.backend(node, None)
            original = backend.verify_restore

            def changed(name, copied):
                with (backend.folder / "archive.dump").open("ab") as stream:
                    stream.write(b"changed-before-restore")
                return original(name, copied)

            backend.verify_restore = changed
            with self.subTest(node=node), self.assertRaises(RuntimeError):
                with guard.hold():
                    backend.guard = guard
                    stop_stateful(
                        node,
                        backend,
                        receipt_path=backend.folder / "negative.receipt.json",
                    )
            self.assertFalse(backend.observe_stopped(node))
            receipt = json.loads((backend.folder / "negative.receipt.json").read_text())
            self.assertEqual(receipt["phase"], "blocked")
            self.assertTrue(
                (backend.folder / "copy.json").exists(),
                "must reach independent copy before corruption",
            )
            self.assertIn(
                b"changed-before-restore",
                (backend.folder / "archive.dump").read_bytes(),
            )
            self.assertEqual(self.store.read()[0]["state"], "ambiguous")
            # A new independent fixture store for the next engine; no production recovery.
            self.store = S3CAS(
                self.client,
                "lab-state",
                "operations/" + scope_key(self.scope) + "-" + node + ".json",
            )
            OperationLease(
                self.store, self.scope, "fixture-bootstrap", "create"
            ).initialize()

    def test_evidence_missing_stale_numeric_and_lab_real_mixing_never_stops(self):
        import copy

        guard = OperationLease(self.store, self.scope, "C", "destroy")
        with guard.hold():
            for mutation in [
                "missing-service",
                "wrong-instance",
                "wrong-issuer",
                "stale",
                "numeric-true",
                "real",
            ]:
                item = evidence(self.inventory, guard, self.key)
                item.pop("signature")
                if mutation == "missing-service":
                    del item["services"]["spark_executors_absent"]
                elif mutation == "wrong-instance":
                    next(iter(item["nodes"].values()))["instance_id"] = "i-wrong"
                elif mutation == "wrong-issuer":
                    item["issuer"] = "unknown"
                elif mutation == "stale":
                    item["observed_at"] -= 60
                elif mutation == "numeric-true":
                    item["services"]["web_admission_closed"] = 1
                else:
                    item["source"] = "host-observations"
                item["signature"] = hmac.new(
                    self.key, canonical(item), hashlib.sha256
                ).hexdigest()
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    validate_shutdown(
                        item,
                        self.inventory,
                        guard.check(),
                        {"local-fixture-observer": self.key},
                    )
