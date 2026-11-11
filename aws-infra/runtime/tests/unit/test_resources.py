import unittest
from unittest.mock import patch

from mlo_runtime.contracts import ROOT, load_inventory
from mlo_runtime.jobs import spark_submit
from mlo_runtime.queries import QUERIES, TRACK2_QUERIES
from mlo_runtime.render import build, compose, service_configs
from mlo_runtime.track2 import proposal


class LowLoadResources(unittest.TestCase):
    def setUp(self):
        self.inventory, self.services = load_inventory(
            ROOT / "aws-infra/contracts/inventory.example.json"
        )

    def test_scheduler_never_advertises_more_cores_than_container_allows(self):
        advertised = 0
        for node in self.inventory["nodes"]:
            if node["role"] != "spark":
                continue
            worker = compose(node, self.inventory)["services"]["spark-worker"]
            argv = worker["entrypoint"]
            cores = int(argv[argv.index("--cores") + 1])
            self.assertLessEqual(cores, worker["cpus"])
            self.assertEqual(cores, 1)
            advertised += cores
            config = service_configs(node, self.inventory)["spark-defaults.conf"]
            self.assertIn("spark.cores.max 1\n", config)
            self.assertIn("spark.dynamicAllocation.enabled false\n", config)
        _, details = build(self.inventory, self.services)
        budget = details["resource_profile"]
        self.assertEqual(budget["cluster_executor_cores"], advertised)
        self.assertEqual(
            advertised - len(budget["continuous_queries"]),
            budget["batch_capacity_with_both_streams_running"],
        )
        self.assertFalse(budget["ready_for_aws"])

    def test_all_queries_are_bounded_and_leave_capacity_for_other_apps(self):
        for query in [*QUERIES, *TRACK2_QUERIES]:
            with self.subTest(query=query), patch("mlo_runtime.track2.validate_artifact"):
                argv = spark_submit(
                    query, "/opt/mlo/current/apps/job.py",
                    ["spark-01:7077", "spark-02:7077", "spark-03:7077"],
                    "10.200.1.11", accepted_artifact={"accepted": True},
                )
                self.assertEqual(argv[argv.index("--executor-cores") + 1], "1")
                self.assertEqual(argv[argv.index("--total-executor-cores") + 1], "1")
                self.assertEqual(argv[argv.index("--driver-memory") + 1], "1g")
                self.assertIn("spark.dynamicAllocation.enabled=false", argv)
        # The handoff must match the command; it may not still ask for two cores
        # when two permanent one-core streams occupy the other worker slots.
        track2 = proposal("dev", "fixture-v1")
        self.assertEqual(track2["queries"]["venues-catalog"]["total_executor_cores"], 1)
        self.assertFalse(track2["accepted"])


if __name__ == "__main__":
    unittest.main()
