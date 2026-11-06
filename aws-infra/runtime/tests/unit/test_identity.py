import copy
import random
from pathlib import Path
import tempfile
import unittest

from mlo_runtime.contracts import ROOT, load_inventory
from mlo_runtime.render import service_configs, compose, systemd_units
from mlo_runtime.install import InstallError, validate_identity


class IdentityTests(unittest.TestCase):
    def test_render_wires_real_supervisor_relay_and_planned_stop_interfaces(self):
        inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        for node in inventory["nodes"]:
            units = systemd_units(node, inventory)
            if node["role"] == "control":
                self.assertIn("mlo_runtime.supervisor --binding /etc/mlo/supervisor.json", units["mlo-supervisor.service"])
            if node["role"] in {"postgresql", "mongodb"}:
                self.assertIn("--confirm-planned-stop " + node["name"], units["mlo-stateful-stop.service"])
            if node["role"] == "gateway":
                relay = compose(node, inventory)["services"]["llm-relay"]
                self.assertEqual(relay["command"], ["python3", "-m", "mlo_runtime.relay", "--binding", "/etc/mlo/relay.json"])
                self.assertEqual(relay["healthcheck"]["test"][-1], "--check")
                self.assertIn("/etc/mlo/relay.json:/etc/mlo/relay.json:ro", relay["volumes"])

    def test_reverse_and_shuffled_inventory_preserves_all_ids_and_voters(self):
        inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        expected = {n["name"]: service_configs(n, inventory) for n in inventory["nodes"] if n["role"] in {"kafka", "spark"}}
        for seed in range(10):
            candidate = copy.deepcopy(inventory)
            random.Random(seed).shuffle(candidate["nodes"])
            if seed == 0:
                candidate["nodes"].reverse()
            for node in candidate["nodes"]:
                if node["name"] in expected:
                    self.assertEqual(service_configs(node, candidate), expected[node["name"]])
                    if node["role"] == "spark":
                        self.assertEqual(compose(node, candidate)["services"]["zookeeper"]["entrypoint"], ["zkServer.sh", "start-foreground"])

    def test_kafka_existing_metadata_preserved_and_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            metadata = data / "kafka/meta.properties"
            metadata.parent.mkdir()
            body = "cluster.id=3Db5Lwr0R6KpX5a1Jq2Z8w\nnode.id=2\nversion=1\n"
            metadata.write_text(body)
            identity = {"node_id": 2, "cluster_id": "3Db5Lwr0R6KpX5a1Jq2Z8w"}
            validate_identity("kafka", "kafka-02", data, identity)
            for change in [{"node_id": 1}, {"cluster_id": "4Db5Lwr0R6KpX5a1Jq2Z8w"}]:
                with self.assertRaises(InstallError):
                    validate_identity("kafka", "kafka-02", data, {**identity, **change})
                self.assertEqual(metadata.read_text(), body)
            metadata.unlink()
            with self.assertRaises(InstallError):
                validate_identity("kafka", "kafka-02", data, identity)
            self.assertFalse(metadata.exists())

    def test_zookeeper_first_myid_reapply_and_mismatch_preservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            validate_identity("spark", "spark-03", data, {"node_id": 3})
            myid = data / "zookeeper/myid"
            old = myid.stat().st_mtime_ns
            validate_identity("spark", "spark-03", data, {"node_id": 3})
            self.assertEqual(myid.stat().st_mtime_ns, old)
            myid.write_text("2\n")
            with self.assertRaises(InstallError):
                validate_identity("spark", "spark-03", data, {"node_id": 3})
            self.assertEqual(myid.read_text(), "2\n")
            myid.unlink()
            (myid.parent / "version-2").mkdir()
            with self.assertRaises(InstallError):
                validate_identity("spark", "spark-03", data, {"node_id": 3})
            self.assertFalse(myid.exists())
