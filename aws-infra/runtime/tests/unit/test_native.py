import unittest

from mlo_runtime.contracts import ROOT, load_inventory
from mlo_runtime.install import InstallError, require_native_acceptance
from mlo_runtime.native import contract, plan


class NativeDesign(unittest.TestCase):
    def setUp(self):
        self.inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        self.value = plan(self.inventory)

    def test_every_data_node_has_native_units_and_no_container_execution(self):
        self.assertEqual(len(self.value["nodes"]), 9)
        self.assertFalse(self.value["ready_for_aws"])
        self.assertTrue(self.value["deployment_blockers"])
        for node in self.value["nodes"].values():
            for text in node["units"].values():
                self.assertNotIn("docker", text)
                self.assertNotIn("compose", text)
                self.assertIn("Type=exec\n", text)
                self.assertIn("User=mlo\n", text)
                self.assertIn("RequiresMountsFor=/srv/mlo ", text)
                self.assertIn("KillMode=control-group\n", text)
                self.assertIn("MemoryMax=", text)
                self.assertIn("CPUQuota=100%\n", text)
                self.assertIn("ConditionPathExists=/etc/mlo-native/accepted-host.json", text)

    def test_persistent_data_and_credentials_resolve_to_host_paths(self):
        for name, node in self.value["nodes"].items():
            if node["role"] == "kafka":
                text = node["configs"]["server.properties"]
                self.assertIn(f"node.id={int(name[-1])}\n", text)
                self.assertIn(f"log.dirs=/srv/mlo/{self.inventory['generation']}/kafka", text)
                self.assertIn("/run/credentials/mlo-kafka.service/", text)
                self.assertNotIn("format", "\n".join(node["units"].values()))
            elif node["role"] == "spark":
                self.assertEqual(node["configs"]["myid"].strip(), str(int(name[-1])))
                self.assertIn("--cores 1 --memory 3g", node["units"]["mlo-spark-worker.service"])
                for component in ("master", "worker"):
                    config = node["configs"][f"{component}/spark-defaults.conf"]
                    self.assertIn(f"/run/credentials/mlo-spark-{component}.service/", config)
                    self.assertNotIn("/run/mlo/secrets", config)
            else:
                config = node["configs"]["elasticsearch.yml"]
                self.assertIn("path.data: /srv/mlo/", config)
                self.assertIn("/run/credentials/mlo-elasticsearch.service/", config)

    def test_real_legacy_deployment_cannot_bypass_native_migration_gate(self):
        for role in ("kafka", "spark", "elasticsearch", "control"):
            with self.subTest(role=role), self.assertRaises(InstallError):
                require_native_acceptance({"role": role, "ready": True, "native_accepted": True})
            require_native_acceptance({"role": role, "lab": True})
        require_native_acceptance({"role": "postgresql", "lab": False})
        self.assertFalse(contract()["ready_for_aws"])


if __name__ == "__main__":
    unittest.main()
