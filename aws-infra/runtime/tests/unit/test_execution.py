"""Regression for Spark 4.1.3's actual master/worker JSON observation shape."""

import copy
import time
import unittest
from unittest.mock import patch

from mlo_runtime.execution import DockerSparkExecution
from mlo_runtime.operations import UnsafeTransition


class SparkObservationTests(unittest.TestCase):
    def setUp(self):
        self.token = {
            "owner": "control-a",
            "generation": "g1",
            "epoch": 1,
            "expires_at": time.time() + 15,
        }
        self.adapter = DockerSparkExecution(
            ("driver1", "driver2"),
            ("m1", "m2", "m3"),
            ("w1", "w2", "w3"),
            "ca",
            "crt",
            "key",
        )
        self.snapshots = {
            "m"
            + str(i): {
                "url": "spark://m" + str(i) + ":7077",
                "status": "ALIVE" if i == 1 else "STANDBY",
                "activeapps": [],
                "activedrivers": [],
                "aliveworkers": 3 if i == 1 else 0,
                "workers": (
                    [{"id": "worker" + str(j), "state": "ALIVE"} for j in (1, 2, 3)]
                    if i == 1
                    else []
                ),
            }
            for i in (1, 2, 3)
        }
        self.snapshots.update(
            {
                "w"
                + str(i): {
                    "id": "worker" + str(i),
                    "masterurl": "spark://m1:7077",
                    "executors": [],
                    "finishedexecutors": [],
                    "coresused": 0,
                    "memoryused": 0,
                }
                for i in (1, 2, 3)
            }
        )

    def observe(self):
        containers = [{"State": {"Running": False, "Status": "exited"}}] * 2
        with patch.object(
            self.adapter, "_containers", return_value=containers
        ), patch.object(
            self.adapter, "_json", side_effect=lambda url: self.snapshots[url]
        ):
            return self.adapter.observe(self.token)

    def test_real_json_without_worker_drivers_can_prove_client_mode_idle(self):
        proof = self.observe()
        proof.validate("control-a", "g1", 1, time.time())

    def test_incomplete_malformed_or_disconnected_snapshots_cannot_prove_idle(self):
        original = copy.deepcopy(self.snapshots)
        mutations = [
            lambda: self.snapshots["w1"].pop("executors"),
            lambda: self.snapshots["w1"].update(executors=None),
            lambda: self.snapshots["w1"].update(coresused=False),
            lambda: self.snapshots["w1"].update(memoryused=512),
            lambda: self.snapshots["w1"].update(masterurl="spark://old:7077"),
            lambda: self.snapshots["w2"].update(id="worker1"),
            lambda: self.snapshots["m1"].update(aliveworkers=2),
            lambda: self.snapshots["m1"]["workers"].pop(),
            lambda: self.snapshots["m2"].update(status="RECOVERING"),
            lambda: self.snapshots["m1"].update(activeapps=[{"id": "remaining-app"}]),
            lambda: self.snapshots["m1"].update(
                activedrivers=[{"id": "remaining-driver"}]
            ),
            lambda: self.snapshots["w1"].update(executors=[{"appid": "remaining-app"}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.snapshots = copy.deepcopy(original)
                mutate()
                with self.assertRaises(UnsafeTransition):
                    self.observe().validate("control-a", "g1", 1, time.time())
