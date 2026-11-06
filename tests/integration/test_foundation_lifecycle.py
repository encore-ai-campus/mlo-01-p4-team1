"""All actual mocked Foundation plans feed the C retention review, without AWS."""

import copy
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "aws-infra/foundation/scripts"),
    str(ROOT / "aws-infra/lifecycle/src"),
]
from lifecycle_export import export_snapshot, audit_removal, sha
from mlo_lifecycle.foundation import review_snapshots, review_root_plan

ROOTS = [
    "encore1",
    "encore2",
    "encore3",
    "encore4",
    "encore5",
    "connectivity",
    "bootstrap",
]


class FoundationLifecycleIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = ROOT / "aws-infra/foundation"
        location = json.loads((base / ".artifacts/latest-run.json").read_text())["logs"]
        cls.snapshots = {}
        for root in ROOTS:
            events = [
                json.loads(line)
                for line in (base / location / (root + "-test.jsonl"))
                .read_text()
                .splitlines()
            ]
            plans = [e["test_plan"] for e in events if e.get("type") == "test_plan"]
            if len(plans) != 1:
                raise AssertionError(
                    "one actual mocked plan per Foundation root required"
                )
            shown = plans[0]
            expected = {
                r["address"]: r["change"]["after"].get("id")
                for r in shown["resource_changes"]
                if r["mode"] == "managed" and r["change"]["after"] is not None
            }
            metadata = {
                "environment": "dev",
                "generation": "local-mock-export",
                "account": root,
                "region": "ap-northeast-2",
                "state_key": f"dev/{root}/terraform.tfstate",
                "lineage": "mock-" + root,
                "serial": 1,
                "expected_targets": expected,
                "observed_at": time.time(),
            }
            cls.snapshots[root] = export_snapshot(
                root, shown, metadata, source="mock-plan"
            )

    def test_full_seven_root_graph_retention_and_purge(self):
        retain = review_snapshots(self.snapshots)
        purge = review_snapshots(self.snapshots, "purge-data")
        self.assertGreater(len(retain["targets"]), 800)
        self.assertEqual(
            sum(r["kind"] == "compute" for r in retain["targets"].values()), 16
        )
        self.assertEqual(
            set(retain["targets"]), set(retain["delete"]) | set(retain["retain"])
        )
        self.assertGreater(len(retain["retain"]), len(purge["retain"]))
        self.assertTrue(
            all(
                r["root"] == "bootstrap"
                for k, r in purge["targets"].items()
                if k in purge["retain"]
            )
        )
        for key in retain["retain"]:
            self.assertTrue(
                set(retain["targets"][key]["dependencies"]) <= set(retain["retain"])
            )
        for value in [retain, purge]:
            self.assertFalse(value["ready_for_aws"])
            self.assertFalse(value["execution_enabled"])

    def test_each_root_saved_plan_exact_partition_and_failure_mutations(self):
        review = review_snapshots(self.snapshots)
        for root in ROOTS:
            snapshot = self.snapshots[root]
            changes = []
            for address, r in snapshot["resources"].items():
                changes.append(
                    {
                        "mode": "managed",
                        "address": address,
                        "change": {
                            "before": {"id": r["id"]},
                            "actions": (
                                ["delete"]
                                if root + ":" + address in review["delete"]
                                else ["no-op"]
                            ),
                        },
                    }
                )
            candidate = {"resource_changes": changes}
            self.assertTrue(
                review_root_plan(review, self.snapshots, root, candidate, "a" * 64)[
                    "accepted"
                ]
            )
            for mutation in ["missing", "duplicate", "replacement", "wrong-id"]:
                other = copy.deepcopy(candidate)
                if mutation == "missing":
                    other["resource_changes"].pop()
                elif mutation == "duplicate":
                    other["resource_changes"].append(
                        copy.deepcopy(other["resource_changes"][0])
                    )
                elif mutation == "replacement":
                    other["resource_changes"][0]["change"]["actions"] = [
                        "delete",
                        "create",
                    ]
                else:
                    other["resource_changes"][0]["change"]["before"][
                        "id"
                    ] = "wrong-physical-id"
                with self.subTest(root=root, mutation=mutation), self.assertRaises(
                    ValueError
                ):
                    review_root_plan(review, self.snapshots, root, other, "a" * 64)

    def test_unknown_generation_digest_and_freshness_fail_closed(self):
        for mutation in ["unknown", "generation", "digest"]:
            other = copy.deepcopy(self.snapshots)
            if mutation == "unknown":
                other["encore2"]["unknown"] = ["unclassified.address"]
            elif mutation == "generation":
                other["encore2"]["binding"]["generation"] = "other"
            else:
                other["encore2"]["resources"].pop(
                    next(iter(other["encore2"]["resources"]))
                )
            if mutation != "digest":
                other["encore2"]["sha256"] = sha(
                    {k: v for k, v in other["encore2"].items() if k != "sha256"}
                )
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                review_snapshots(other)

    def test_observed_state_requires_raw_lineage_serial_ids(self):
        snapshot = self.snapshots["encore2"]
        selected = next(iter(snapshot["resources"].values()))
        address = selected["address"]
        unindexed = address.split(".")
        # A supplied expanded address from the actual mock graph, an observed ID
        # and a matching raw-state instance; never treated as real AWS evidence.
        typ = selected["type"]
        name = address.rsplit(".", 1)[1].split("[")[0]
        module = address[: address.rfind(typ + ".")].rstrip(".")
        item = {
            "address": address,
            "mode": "managed",
            "type": typ,
            "name": name,
            "values": {"id": "local-observed-id"},
        }
        shown = {
            "format_version": "1.0",
            "values": {"root_module": {"resources": [item]}},
        }
        import re

        tail = address.rsplit(".", 1)[1]
        index = re.search(r"\[(.*)\]$", tail)
        instance = {"attributes": {"id": "local-observed-id"}}
        if index:
            instance["index_key"] = json.loads(index.group(1))
        raw = {
            "lineage": "local-state",
            "serial": 9,
            "resources": [
                {
                    "mode": "managed",
                    "module": module,
                    "type": typ,
                    "name": name,
                    "instances": [instance],
                }
            ],
        }
        metadata = {
            **snapshot["binding"],
            "lineage": "local-state",
            "serial": 9,
            "observed_at": time.time(),
            "expected_targets": {address: "local-observed-id"},
        }
        observed = export_snapshot("encore2", shown, metadata, raw_state=raw)
        self.assertEqual(observed["source"], "state")
        self.assertFalse(observed["ready_for_aws"])
        for field, value in [("lineage", "wrong"), ("serial", 10)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                export_snapshot(
                    "encore2", shown, metadata, raw_state={**raw, field: value}
                )
        with self.assertRaises(ValueError):
            export_snapshot(
                "encore2",
                shown,
                {**metadata, "observed_at": time.time() - 301},
                raw_state=raw,
            )
        with self.assertRaises(ValueError):
            export_snapshot("encore2", shown, metadata)
