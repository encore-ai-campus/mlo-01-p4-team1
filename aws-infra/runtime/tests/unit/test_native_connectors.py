"""Local-only negative tests for immutable Spark connector closure."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mlo_runtime.contracts import ROOT, load_inventory
from mlo_runtime.native import plan
from mlo_runtime.native_connectors import (
    classpath, locked, lock_sha256, release_id, verify_jars,
)
from mlo_runtime.native_releases import lock

CONNECTOR_LOCK = ROOT / "aws-infra/runtime/native-connectors.lock.json"
NATIVE_RELEASE_LOCK = ROOT / "aws-infra/runtime/native-releases.lock.json"


class NativeConnectorContract(unittest.TestCase):
    def test_distribution_and_extra_closure_are_pinned_without_duplicates(self):
        value = locked(CONNECTOR_LOCK)
        distribution = {x["filename"]: x["sha256"] for x in value["distribution_jars"]}
        extras = {x["filename"]: x["sha256"] for x in value["jars"]}
        self.assertEqual((len(distribution), len(extras)), (286, 15))
        self.assertFalse(set(distribution) & set(extras))
        self.assertEqual(len(distribution | extras), 301)
        self.assertIn("lz4-java-1.8.0.jar", distribution)
        self.assertNotIn("lz4-java-1.8.0.jar", extras)

    def test_lock_rejects_duplicate_or_unreviewed_connector(self):
        value = locked(CONNECTOR_LOCK)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "lock.json"
            value["jars"][0]["filename"] = value["distribution_jars"][0]["filename"]
            out.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                locked(out)

    def test_exact_jar_closure_refuses_unknown_and_tampered_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p = root / "approved-1.jar"
            p.write_bytes(b"approved")
            entries = [{"filename": "approved-1.jar", "sha256": hashlib.sha256(b"approved").hexdigest()}]
            self.assertEqual(verify_jars(root, entries)["files"], 1)
            unknown = root / "unreviewed-1.jar"
            unknown.write_bytes(b"unknown")
            with self.assertRaisesRegex(ValueError, "closure"):
                verify_jars(root, entries)
            unknown.unlink()
            p.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify_jars(root, entries)

    def test_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            approved = root / "approved-1.jar"
            approved.write_bytes(b"ok")
            entries = [{"filename": "approved-1.jar", "sha256": hashlib.sha256(b"ok").hexdigest()}]
            (root / "alias-1.jar").symlink_to(approved)
            with self.assertRaisesRegex(ValueError, "linked"):
                verify_jars(root, entries)

    def test_connector_lock_changes_effective_spark_release(self):
        spark = lock(NATIVE_RELEASE_LOCK)["releases"]["spark"]["digest"]
        current = lock_sha256(CONNECTOR_LOCK)
        first = release_id(spark, current)
        second = release_id(spark, "0" * 64)
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 24)
        self.assertEqual(
            classpath("/opt/mlo/native", first),
            "/opt/mlo/native/connectors/spark-" + first + "/jars/*",
        )

    def test_all_three_spark_workers_have_same_fixed_driver_executor_classpath(self):
        inv, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        value = plan(inv, "connector-lock-test")
        nodes = [n for n in value["nodes"].values() if n["role"] == "spark"]
        self.assertEqual(len(nodes), 3)
        self.assertEqual(len({n["spark_runtime_release_id"] for n in nodes}), 1)
        self.assertEqual(len({n["native_connector_lock_sha256"] for n in nodes}), 1)
        for node in nodes:
            cp = node["connector_classpath"]
            self.assertEqual(node["native_connector_lock_sha256"], lock_sha256(CONNECTOR_LOCK))
            for component in ("master", "worker"):
                config = node["configs"][component + "/spark-defaults.conf"]
                self.assertIn("spark.driver.extraClassPath " + cp + "\n", config)
                self.assertIn("spark.executor.extraClassPath " + cp + "\n", config)
                self.assertNotIn("spark.jars.packages", config)


    def test_every_spark_master_worker_has_immutable_jar_prestart_guard(self):
        import hashlib
        expected_hash = hashlib.sha256((ROOT / "aws-infra/runtime/src/mlo_runtime/native_connector_guard.py").read_bytes()).hexdigest()
        inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        result = plan(inventory, "connector-guard-test")
        sparks = [node for node in result["nodes"].values() if node["role"] == "spark"]
        self.assertEqual(len(sparks), 3)
        for node in sparks:
            for component in ("master", "worker"):
                body = node["units"]["mlo-spark-" + component + ".service"]
                lines = [line for line in body.splitlines() if line.startswith("ExecStartPre=")]
                matching = [line for line in lines if "native_connector_guard.py" in line]
                self.assertEqual(len(matching), 1)
                self.assertIn(expected_hash, matching[0])
                self.assertIn(node["spark_runtime_release_id"], matching[0])
                self.assertIn("native-connectors.lock.json", matching[0])
                self.assertIn("native-releases.lock.json", matching[0])
                self.assertTrue(matching[0].startswith("ExecStartPre=+/usr/bin/python3 "))
            self.assertNotIn("native_connector_guard.py", node["units"]["mlo-zookeeper.service"])

    def test_standalone_systemd_jar_guard_rejects_extra_and_modified_files(self):
        from mlo_runtime.native_connector_guard import verify_files
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            good = base / "approved.jar"
            good.write_bytes(b"original")
            pin = [{"filename": good.name, "sha256": hashlib.sha256(b"original").hexdigest()}]
            verify_files(base, pin)
            (base / "unknown.jar").write_bytes(b"extra")
            with self.assertRaises(ValueError):
                verify_files(base, pin)
            (base / "unknown.jar").unlink()
            good.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                verify_files(base, pin)


if __name__ == "__main__":
    unittest.main()
