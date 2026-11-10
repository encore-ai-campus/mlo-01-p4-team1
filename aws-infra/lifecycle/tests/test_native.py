"""Native ordering and fail-closed journal tests; no systemd/AWS claim."""
import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from mlo_lifecycle.model import ROOT
sys.path[:0] = [str(ROOT / "aws-infra/contracts"), str(ROOT / "aws-infra/runtime/src")]
from infra_common.coordination import canonical
from infra_common.evidence import SERVICES
from mlo_lifecycle.native import stop_native, recover_native
from mlo_runtime.contracts import load_inventory
from mlo_runtime.native import plan


class Guard:
    def __init__(self, inventory, operation):
        self.operation = operation
        self.token = {"operation_id": "native-test", "epoch": 1, "operation": operation,
                      "scope": {"generation": inventory["generation"]}}
    def check(self):
        return self.token


class Backend:
    def __init__(self, name, generation, calls):
        self.binding = {"node": name, "generation": generation}
        self.name, self.calls = name, calls
    def mount(self):
        pass
    def stop(self, node):
        self.calls.extend(("stop", self.name, n) for n in node["units"])
    def stopped(self, units):
        pass
    def start(self, node):
        self.calls.extend(("start", self.name, n) for n in node["units"])
    def health(self, node):
        return {"ready": True}


class NativeLifecycle(unittest.TestCase):
    def setUp(self):
        self.inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        self.targets = plan(self.inventory)["nodes"]
        self.calls = []
        self.backends = {n: Backend(n, self.inventory["generation"], self.calls) for n in self.targets}
        self.key = b"synthetic-native-test-issuer-key-32bytes"

    def evidence(self):
        now = time.time()
        proof = {"schema_version": "runtime-shutdown-evidence-v1", "source": "local-lab", "operation_id": "native-test", "epoch": 1,
                 "generation": self.inventory["generation"], "issuer": "test", "observed_at": now, "expires_at": now + 20,
                 "nodes": {n["name"]: {"instance_id": n["instance_id"], "generation": self.inventory["generation"]} for n in self.inventory["nodes"]},
                 "services": {s: True for s in SERVICES}}
        proof["signature"] = hmac.new(self.key, canonical(proof), hashlib.sha256).hexdigest()
        return proof

    def test_all_spark_daemons_stop_before_any_zookeeper_and_no_writer_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            result = stop_native(self.inventory, self.targets, self.backends, Guard(self.inventory, "destroy"), self.evidence, {"test": self.key}, folder)
            self.assertTrue(result["complete"])
            zk = [i for i, c in enumerate(self.calls) if c[2] == "mlo-zookeeper.service"]
            spark = [i for i, c in enumerate(self.calls) if c[2] in {"mlo-spark-master.service", "mlo-spark-worker.service"}]
            self.assertLess(max(spark), min(zk))
            with self.assertRaises(ValueError):
                stop_native(self.inventory, self.targets, self.backends, Guard(self.inventory, "destroy"), self.evidence, {"test": self.key}, folder)
        self.calls.clear()
        result = recover_native(self.targets, self.backends, Guard(self.inventory, "deploy"), lambda: True)
        self.assertFalse(result["writers_admitted"])
        self.assertTrue(all(c[2] == "mlo-zookeeper.service" for c in self.calls[:3]))

    def test_bad_evidence_or_unverified_restore_never_starts_or_stops(self):
        proof = self.evidence()
        proof["services"]["spark_executors_absent"] = False
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            stop_native(self.inventory, self.targets, self.backends, Guard(self.inventory, "destroy"), proof, {"test": self.key}, folder)
        with self.assertRaises(ValueError):
            recover_native(self.targets, self.backends, Guard(self.inventory, "deploy"), lambda: False)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
