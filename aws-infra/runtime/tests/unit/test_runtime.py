import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mlo_runtime.contracts import (
    ROOT,
    ContractError,
    load_inventory,
    validate_inventory,
    validate_services,
)
from mlo_runtime.render import (
    adapter,
    build,
    compose,
    no_proxy,
    render,
    service_configs,
    QUERIES,
)
from mlo_runtime.operations import (
    ActiveGroup,
    FenceEvidence,
    UnsafeTransition,
    backup,
    cancel_status,
    local_lock,
    rollout,
    Request,
    stop_stateful,
)
from mlo_runtime.jobs import spark_submit


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ROOT / "aws-infra/contracts/inventory.example.json"
        self.inventory, self.services = load_inventory(self.fixture)

    def test_inventory_rejects_mutations(self):
        mutations = [
            lambda i: i["nodes"].append(i["nodes"][0]),
            lambda i: i["nodes"][0].update(role="gateway"),
            lambda i: i["nodes"][0].update(subnet_kind="public"),
            lambda i: i["secret_refs"].pop("kafka"),
            lambda i: i.update(extra_password="do-not-echo"),
        ]
        for mutation in mutations:
            data = copy.deepcopy(self.inventory)
            mutation(data)
            with self.subTest(mutation=mutation), self.assertRaises(ContractError):
                validate_inventory(data)

    def test_public_and_port_collision_rejected(self):
        for service, changes in [
            ("web", {"exposure": "public"}),
            ("web", {"port": 3129}),
        ]:
            data = copy.deepcopy(self.services)
            data["services"][service].update(changes)
            with self.assertRaises(ContractError):
                validate_services(data, self.inventory)

    def test_generation_mismatch_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "plan.json"
            with self.assertRaises(ContractError):
                render(self.fixture, target, expected_generation="another")
            self.assertFalse(target.exists())

    def test_profiles_and_regions_are_per_host(self):
        result = adapter(self.inventory)
        hosts = {
            k: v
            for group in result["all"]["children"].values()
            for k, v in group["hosts"].items()
        }
        self.assertEqual(len(hosts), 16)
        for node in self.inventory["nodes"]:
            self.assertEqual(
                hosts[node["name"]]["ansible_aws_ssm_profile"], "p4-controller-" + node["account"]
            )
            self.assertEqual(
                hosts[node["name"]]["ansible_aws_ssm_region"], "ap-northeast-2"
            )

    def test_details_bind_secrets_ports_and_timers(self):
        plan, details = build(self.inventory, self.services)
        self.assertFalse(plan["ready_for_aws"])
        self.assertTrue(details["unresolved"])
        self.assertEqual(
            details["nodes"]["control-a"]["timers"]["feedback-analytics-batch"],
            "*:0/15",
        )
        self.assertEqual(
            details["nodes"]["gateway-b"]["weather_collector"], "standby-manual"
        )
        self.assertNotIn("mongodb", details["nodes"]["gateway-a"]["secret_refs"])
        ports = [port for pair in QUERIES.values() for port in pair]
        self.assertEqual(len(set(ports)), 6)
        self.assertTrue(all(19000 <= p <= 19099 for p in ports))

    def test_proxy_bypass_all_internal_hosts_and_metadata(self):
        bypass = no_proxy(self.inventory).split(",")
        for value in [
            "169.254.169.254",
            "ssmmessages.ap-northeast-2.amazonaws.com",
            ".s3.ap-northeast-2.amazonaws.com",
            ".ts.net",
        ]:
            self.assertIn(value, bypass)
        self.assertTrue(all(n["private_ip"] in bypass for n in self.inventory["nodes"]))

    def test_compose_has_limits_persistence_and_real_health_checks(self):
        for node in self.inventory["nodes"]:
            for name, spec in compose(node, self.inventory)["services"].items():
                with self.subTest(node=node["name"], service=name):
                    self.assertEqual(spec["platform"], "linux/amd64")
                    self.assertIn("mem_limit", spec)
                    self.assertIn("pids_limit", spec)
                    self.assertNotIn("ports", spec)
                    self.assertNotIn("true", spec["healthcheck"]["test"])
                    self.assertTrue(
                        any("/run/mlo/secrets" in v for v in spec["volumes"])
                    )

    def test_tls_configs_and_no_auto_kafka_format(self):
        for node in self.inventory["nodes"]:
            files = service_configs(node, self.inventory)
            if node["role"] == "kafka":
                self.assertIn("ssl.client.auth=required", files["server.properties"])
                self.assertIn(node["private_ip"], files["server.properties"])
                self.assertNotIn("format", json.dumps(compose(node, self.inventory)))
            if node["role"] == "mongodb":
                self.assertIn("requireTLS", files["mongod.yaml"])


class MemoryLease:
    def __init__(self, token=None):
        self.token = token
        self.reject = False

    def read(self):
        return self.token

    def cas(self, old, new):
        if self.reject or old != self.token:
            return False
        self.token = new
        return True


class Execution:
    def __init__(self):
        self.proof = FenceEvidence(
            "control-a", "fixture-v1", 7, 100, True, True, True, True, True, True
        )
        self.stops = 0
        self.fail = False

    def observe(self, token):
        return self.proof

    def stop_group(self, token):
        self.stops += 1
        if self.fail:
            raise UnsafeTransition("container stop failure")


class FencingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.token = {
            "owner": "control-a",
            "generation": "fixture-v1",
            "epoch": 7,
            "expires_at": 99,
        }
        self.lease = MemoryLease(self.token)
        self.execution = Execution()
        self.group = ActiveGroup(
            self.path,
            "fixture-v1",
            "control-b",
            self.lease,
            self.execution,
            clock=lambda: 100,
        )

    def test_expiry_alone_never_allows_takeover(self):
        for field in [
            "containers_stopped",
            "drivers_absent",
            "executors_absent",
            "cluster_idle",
            "all_masters_observed",
            "all_workers_observed",
        ]:
            self.execution.proof = replace(Execution().proof, **{field: False})
            with self.subTest(field=field), self.assertRaises(UnsafeTransition):
                self.group.acquire()
            self.assertEqual(self.lease.token, self.token)

    def test_future_stale_other_owner_and_epoch_rejected(self):
        for mutation in [
            {"observed_at": 101},
            {"observed_at": 89},
            {"owner": "control-b"},
            {"epoch": 8},
            {"generation": "other"},
        ]:
            self.execution.proof = replace(Execution().proof, **mutation)
            with self.assertRaises(UnsafeTransition):
                self.group.acquire()

    def test_live_owner_and_cas_race_blocked(self):
        self.lease.token = {**self.token, "expires_at": 101}
        with self.assertRaises(UnsafeTransition):
            self.group.acquire()
        self.lease.token = self.token
        self.lease.reject = True
        with self.assertRaises(UnsafeTransition):
            self.group.acquire()

    def test_takeover_records_old_owner(self):
        new = self.group.acquire()
        saved = json.loads(self.path.read_text())
        self.assertEqual(new["epoch"], 8)
        self.assertEqual(saved["old_owner"], self.token)

    def test_loss_fences_real_group_and_preserves_old_owner(self):
        with self.assertRaises(UnsafeTransition):
            self.group.tick(self.token)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["phase"], "fenced")
        self.assertEqual(saved["old_owner"], self.token)
        self.assertEqual(self.execution.stops, 1)

    def test_stop_failure_leaves_fencing_record(self):
        self.execution.fail = True
        with self.assertRaises(UnsafeTransition):
            self.group.tick(self.token)
        self.assertEqual(json.loads(self.path.read_text())["phase"], "fencing")

    def test_host_timer_lock_rejects_overlap(self):
        with local_lock(str(self.path) + ".timer"):
            with self.assertRaises(UnsafeTransition):
                with local_lock(str(self.path) + ".timer"):
                    self.fail("overlapping timer entered")


class Backend:
    def __init__(self):
        self.revisions = {"a": "old", "b": "old"}
        self.targets = set(self.revisions)
        self.events = []

    def snapshot(self, n):
        return self.revisions[n]

    def check_migration(self, n, old, new):
        pass

    def drain(self, n):
        self.targets.discard(n)
        self.events.append((n, "drain"))

    def wait_drained(self, n):
        pass

    def replace_web(self, n, revision):
        self.revisions[n] = revision
        self.events.append((n, revision))

    def wait_ready(self, n, revision):
        if n == "b" and revision == "bad":
            raise RuntimeError("readiness failed")

    def register(self, n):
        self.targets.add(n)

    def wait_healthy(self, n):
        pass


class OperationTests(unittest.TestCase):
    def test_backup_failure_blocks_planned_stateful_stop(self):
        class Backend:
            stopped = False

            def backup(self, node):
                return {"verified": True, "remote_copy_verified": False}

            def stop(self, node):
                self.stopped = True

        backend = Backend()
        with self.assertRaises(UnsafeTransition):
            stop_stateful("postgresql", backend)
        self.assertFalse(backend.stopped)

    def test_spark_driver_ports_have_no_retry_collision_escape(self):
        for query, ports in QUERIES.items():
            command = spark_submit(
                query,
                "/opt/mlo/current/apps/job.py",
                ["spark-01:7077", "spark-02:7077", "spark-03:7077"],
                "10.200.1.11",
            )
            self.assertIn("spark.driver.port=" + str(ports[0]), command)
            self.assertIn("spark.blockManager.port=" + str(ports[1]), command)
            self.assertIn("spark.port.maxRetries=0", command)
            self.assertIn("spark.ui.enabled=false", command)
            self.assertIn(
                f"spark.executor.port={19006 + list(QUERIES).index(query)}", command
            )
        with self.assertRaises(UnsafeTransition):
            spark_submit("weather-stream", "/tmp/unaccepted.py", [], "127.0.0.1")

    def test_incompatible_migration_does_not_drain_healthy_targets(self):
        backend = Backend()

        def refuse(*args):
            raise UnsafeTransition("migration is incompatible")

        backend.check_migration = refuse
        with self.assertRaises(UnsafeTransition):
            rollout(["a", "b"], "new", backend)
        self.assertEqual(backend.events, [])

    def test_partial_web_rollout_restores_both_previous_revisions(self):
        backend = Backend()
        with self.assertRaises(UnsafeTransition):
            rollout(["a", "b"], "bad", backend)
        self.assertEqual(backend.revisions, {"a": "old", "b": "old"})
        self.assertEqual(backend.targets, {"a", "b"})
        self.assertLess(
            backend.events.index(("a", "bad")), backend.events.index(("b", "bad"))
        )

    def test_backup_partial_failure_preserves_prior_data(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = Path(directory) / "previous.dump"
            previous.write_bytes(b"keep-me")
            target = Path(directory) / "new.dump"
            with self.assertRaises(subprocess.CalledProcessError):
                backup(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stdout.write('partial'); sys.exit(3)",
                    ],
                    target,
                    lambda p: True,
                )
            self.assertFalse(target.exists())
            self.assertEqual(previous.read_bytes(), b"keep-me")
            self.assertEqual(list(Path(directory).glob(".partial-*")), [])
            backup(
                [sys.executable, "-c", "print('complete')"],
                target,
                lambda p: p.read_bytes() == b"complete\n",
            )
            with self.assertRaises(UnsafeTransition):
                backup(
                    [sys.executable, "-c", "print('overwrite')"], target, lambda p: True
                )

    def test_unverified_and_empty_backup_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for script in ["pass", "print('data')"]:
                target = Path(directory) / "failed.dump"
                with self.assertRaises(UnsafeTransition):
                    backup([sys.executable, "-c", script], target, lambda p: False)
                self.assertFalse(target.exists())

    def test_cancel_ack_is_not_gpu_release(self):
        self.assertEqual(
            cancel_status({"state": "cancelled"}),
            {"acknowledged": True, "gpu_slot_released": False},
        )

    def test_model_payload_rejects_credentials(self):
        with self.assertRaises(UnsafeTransition):
            Request("id", 100, "r1", ("a",), {"db_password": "never-send"}).body()


if __name__ == "__main__":
    unittest.main()
