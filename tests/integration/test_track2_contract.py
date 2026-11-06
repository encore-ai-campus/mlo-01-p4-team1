"""Use evaluated A Terraform output, then actual B/C consumers; no AWS/app claim."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import datetime

ROOT = Path(__file__).resolve().parents[2]
for source in ("aws-infra/contracts", "aws-infra/runtime/src", "aws-infra/lifecycle/src", "aws-infra/lifecycle/tests"):
    sys.path.insert(0, str(ROOT/source))
from infra_common.track2 import digest, foundation_binding, observation_archive
from mlo_runtime.track2 import accept_foundation as runtime_accept, writer_tables
from mlo_runtime.render import render
from mlo_runtime.observability import sanitize
from mlo_runtime.queries import QUERIES, TRACK2_QUERIES, validate_ports
from mlo_runtime.jobs import run_job
from mlo_runtime.operations import UnsafeTransition
from mlo_lifecycle.track2 import accept_foundation as lifecycle_accept, accept_observation_archive
from mlo_lifecycle.recovery import outputs
from track2_support import track2_fixture


class Track2Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        foundation = ROOT/"aws-infra/foundation"
        run = json.loads((foundation/".artifacts/latest-run.json").read_text())["logs"]
        cls.folder = foundation/run
        cls.inventory = json.loads((cls.folder/"track2-inventory.mock.json").read_text())
        cls.sidecar = json.loads((cls.folder/"track2-sidecar.mock.json").read_text())
        if cls.inventory["example_only"] is not True:
            raise ValueError("mandatory fixture-only input")

    def spec(self):
        spec = track2_fixture()["track2"]
        spec = json.loads(json.dumps(spec).replace(spec["source_generation"], self.inventory["generation"]))
        for asset in spec["assets"].values():
            asset["sha256"] = digest(asset["payload"])
        return spec

    def test_unaccepted_operational_hooks_cannot_be_enabled_by_job_flags(self):
        with self.assertRaises(UnsafeTransition):
            run_job("venues-catalog", Path("not-an-operational-binding.json"))

    def test_evaluated_foundation_consumed_by_b_render_and_c_recovery(self):
        bridge = runtime_accept(self.sidecar, self.inventory)
        self.assertEqual(bridge, lifecycle_accept(self.spec(), self.sidecar, self.inventory))
        self.assertEqual(bridge["writer_tables"], writer_tables(self.sidecar["dataset_version"]))
        self.assertEqual(set(bridge["queries"]), set(QUERIES)|set(TRACK2_QUERIES))
        validate_ports()
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)
            render(self.folder/"track2-inventory.mock.json", path/"plan.json", path/"bundle",
                   foundation_track2=self.folder/"track2-sidecar.mock.json")
            for node in self.inventory["nodes"]:
                actual=json.loads((path/"bundle/hosts"/node["name"]/"track2-bridge.json").read_text())
                self.assertEqual(actual, bridge)
                self.assertFalse(actual["ready_for_aws"])
                self.assertEqual(actual["operational_hooks"], "not_accepted")
        recovered = outputs(self.spec(), rollback=True)
        self.assertEqual(set(recovered["checkpoints"]["queries"]), set(bridge["queries"]))
        self.assertNotIn("venue-b", [d["entity_id"] for d in recovered["alias"]["documents"]])
        self.assertFalse(recovered["alias"]["writers_started"])

    def test_mixed_generation_path_query_owner_and_fixture_rejected(self):
        mutations = [lambda s:s.update(generation="other"), lambda s:s.update(inventory_sha256="0"*64),
                     lambda s:s.update(example_only=False), lambda s:s.update(ready_for_aws=True),
                     lambda s:s.update(driver_queries={"spark-01":["venues-catalog"]}),
                     lambda s:s.update(driver_queries={"control-a":["venues-import"]}),
                     lambda s:s["paths"].update(lake_venues="lake/venues/bronze/v1/"),
                     lambda s:s["paths"]["checkpoints"].update({"control-a":["aws/dev/shared/checkpoint/"]}),
                     lambda s:s.update(unknown=True)]
        for mutate in mutations:
            value=copy.deepcopy(self.sidecar); mutate(value)
            for accept in (foundation_binding, runtime_accept):
                with self.subTest(mutate=mutate, accept=accept), self.assertRaises(ValueError):
                    accept(value, self.inventory)
        bad=self.spec(); bad["dataset_version"]="other-dataset"
        with self.assertRaises(ValueError):
            lifecycle_accept(bad,self.sidecar,self.inventory)

    def test_b_sanitized_emissions_archive_to_c_without_stage_overwrite(self):
        run, business = str(uuid.uuid4()), str(uuid.uuid4())
        events=[]
        for stage in ("SENT","ACKNOWLEDGED","COMMITTED","SEARCHABLE","PUBLISHED"):
            events.append(sanitize({"@timestamp":datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "event":{"id":str(uuid.uuid4()),"dataset":"mlo.pipeline","action":"fixture.transport","outcome":"success"},
                "service":{"name":"fixture"},"host":{"name":"control-a"},
                "cloud":{"provider":"aws","region":"ap-northeast-2","account":{"name":"encore1"}},
                "pipeline":{"name":"venues-catalog","stage":"transport","state":stage,"run_id":run,
                            "business_event_id":business,"release_id":"release-one","attempt":1},
                "message":"private-must-be-removed"}))
        result=accept_observation_archive(events+[events[0]], expected_sha256=digest(events+[events[0]]), version_id="local-synthetic-version")
        self.assertEqual(len(result["events"]),5)
        self.assertEqual({e["business_event_id"] for e in result["events"]},{business})
        self.assertNotIn("private",json.dumps(result))
        reused=copy.deepcopy(events); reused[1]["event"]["id"]=reused[0]["event"]["id"]
        with self.assertRaises(ValueError): observation_archive(reused)
        with self.assertRaises(ValueError):
            accept_observation_archive(events, expected_sha256="0"*64, version_id="local-synthetic-version")
        with self.assertRaises(ValueError):
            accept_observation_archive(events, expected_sha256=digest(events), version_id="")
