"""Saved-plan execution port, using synthetic state and an injected process port.

Tests never authenticate or invoke an AWS provider. The separate Terraform test
uses only the built-in terraform_data resource in a disposable local directory.
"""

import copy
import hashlib
import hmac
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import unittest

from test_preauth_boundary import PrivateInputs, ROOT, digest

sys.path[:0] = [str(ROOT / "aws-infra/lifecycle/src")]
from infra_common import preauth_execution as execution
from infra_common.coordination import canonical
from infra_common.evidence import SERVICES
from lifecycle_export import export_snapshot
from mlo_lifecycle.foundation import review_snapshots
import infra_control


class SavedPlanExecution(PrivateInputs):
    def setUp(self):
        super().setUp()
        location = json.loads(
            (ROOT / "aws-infra/foundation/.artifacts/latest-run.json").read_text()
        )["logs"]
        self.snapshots = {}
        self.raw = {}
        for root in (
            "encore1",
            "encore2",
            "encore3",
            "encore4",
            "encore5",
            "bootstrap",
            "connectivity",
        ):
            events = [
                json.loads(line)
                for line in (
                    ROOT / "aws-infra/foundation" / location / (root + "-test.jsonl")
                )
                .read_text()
                .splitlines()
            ]
            plan = next(e["test_plan"] for e in events if e.get("type") == "test_plan")
            shown_resources = []
            raw_resources = []
            for change in plan["resource_changes"]:
                if change["mode"] != "managed" or change["change"]["after"] is None:
                    continue
                values = copy.deepcopy(change["change"]["after"])
                if not values.get("id"):
                    values["id"] = (
                        "local-observed-"
                        + hashlib.sha256(change["address"].encode()).hexdigest()[:20]
                    )
                if change["type"] == "aws_instance":
                    node = json.loads(
                        re.search(r"\[(.*)\]$", change["address"]).group(1)
                    )
                    next(n for n in self.inventory["nodes"] if n["name"] == node)[
                        "instance_id"
                    ] = values["id"]
                shown_resources.append(
                    {
                        "address": change["address"],
                        "mode": "managed",
                        "type": change["type"],
                        "name": change["name"],
                        "values": values,
                    }
                )
                instance = {"attributes": values}
                tail = change["address"].rsplit(".", 1)[1]
                indexed = re.search(r"\[(.*)\]$", tail)
                if indexed:
                    instance["index_key"] = json.loads(indexed.group(1))
                module = change["address"][
                    : change["address"].rfind(change["type"] + ".")
                ].rstrip(".")
                raw_resources.append(
                    {
                        "mode": "managed",
                        "module": module,
                        "type": change["type"],
                        "name": change["name"],
                        "instances": [instance],
                    }
                )
            shown = {
                "format_version": "1.0",
                "values": {"root_module": {"resources": shown_resources}},
            }
            raw = {
                "lineage": "local-test-" + root,
                "serial": 1,
                "resources": raw_resources,
            }
            metadata = {
                "environment": "dev",
                "generation": "foundation-mock-v1",
                "account": root,
                "region": "ap-northeast-2",
                "state_key": "dev/" + root + "/terraform.tfstate",
                "lineage": raw["lineage"],
                "serial": 1,
                "expected_targets": {
                    r["address"]: r["values"]["id"] for r in shown_resources
                },
                "observed_at": time.time(),
            }
            self.snapshots[root] = export_snapshot(
                root, shown, metadata, raw_state=raw, source="state"
            )
            self.raw[root] = raw
        partition = review_snapshots(self.snapshots, "retain-data")
        self.root = "encore2"
        self.delete = [
            a.split(":", 1)[1]
            for a in partition["delete"]
            if a.startswith(self.root + ":")
        ]
        self.retained = [
            a.split(":", 1)[1]
            for a in partition["retain"]
            if a.startswith(self.root + ":")
        ]
        b = {**self.binding, "example_only": False}
        self.candidate = {
            "variables": {"binding": {"value": b}},
            "resource_changes": [
                {
                    "mode": "managed",
                    "address": a,
                    "change": {
                        "actions": ["delete"] if a in self.delete else ["no-op"],
                        "before": {"id": r["id"]},
                    },
                }
                for a, r in self.snapshots[self.root]["resources"].items()
            ],
        }
        self.token = {
            "operation": "destroy",
            "operation_id": "local-test-destroy",
            "epoch": 2,
            "scope": infra_control.scope(self.binding),
        }
        self.issuer_key = b"LOCAL-ONLY-OBSERVER-TEST-KEY" * 2
        now = time.time()
        shutdown = {
            "schema_version": "runtime-shutdown-evidence-v1",
            "source": "host-observations",
            "operation_id": self.token["operation_id"],
            "epoch": 2,
            "generation": "foundation-mock-v1",
            "nodes": {
                n["name"]: {
                    "instance_id": n["instance_id"],
                    "generation": "foundation-mock-v1",
                }
                for n in self.inventory["nodes"]
            },
            "services": dict.fromkeys(SERVICES, True),
            "issuer": "local-observer",
            "observed_at": now,
            "expires_at": now + 30,
        }
        backup = {
            "schema_version": "stateful-restore-evidence-v1",
            "issuer": "local-observer",
            "generation": "foundation-mock-v1",
            "operation_id": self.token["operation_id"],
            "epoch": 2,
            "observed_at": now,
            "expires_at": now + 30,
            "databases": {
                name: {
                    "instance_id": next(
                        n["instance_id"]
                        for n in self.inventory["nodes"]
                        if n["name"] == name
                    ),
                    "stopped": True,
                    "backup_version": "local-test-version",
                    "backup_sha256": "a" * 64,
                    "independent_failure_domain": True,
                    "restore_verified": True,
                }
                for name in ("postgresql", "mongodb")
            },
        }

        def signed(body):
            return {
                **body,
                "signature": hmac.new(
                    self.issuer_key, canonical(body), hashlib.sha256
                ).hexdigest(),
            }

        files = {
            "plan": b"LOCAL-TEST-SAVED-PLAN-NOT-AWS",
            "plan_json": self.candidate,
            "inventory": self.inventory,
            "snapshots": self.snapshots,
            "trusted_issuers": {"local-observer": self.issuer_key.hex()},
            "runtime_evidence": signed(shutdown),
            "backup_evidence": signed(backup),
            "coordination_contract": {},
        }
        self.payload = {
            "root": str(
                ROOT / "aws-infra/foundation/terraform/environments/dev" / self.root
            ),
            "delete": self.delete,
            "retained": self.retained,
            "mode": "retain-data",
            "terraform": str(ROOT / "aws-infra/foundation/.cache/toolchain/terraform"),
            "current_state_sha256": hashlib.sha256(
                canonical(self.raw[self.root])
            ).hexdigest(),
            "coordination_scope": self.token["scope"],
        }
        for name, value in files.items():
            path = self.put(name + ".json", value)
            self.payload[name] = str(path)
            if name not in {"runtime_evidence", "backup_evidence"}:
                self.payload[name + "_sha256"] = digest(path)
        self.guard = type(
            "Guard", (), {"operation": "destroy", "check": lambda _: self.token}
        )()
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1:] == ["state", "pull"]:
            value = self.raw[self.root]
        elif argv[1:3] == ["show", "-json"] and len(argv) > 3:
            value = self.candidate
        elif argv[1] == "apply":
            value = {}
        elif argv[1:] == ["show", "-json"]:
            value = {
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "mode": "managed",
                                "address": a,
                                "values": {
                                    "id": self.snapshots[self.root]["resources"][a][
                                        "id"
                                    ]
                                },
                            }
                            for a in self.retained
                        ]
                    }
                }
            }
        else:
            raise AssertionError("unexpected process request")
        return subprocess.CompletedProcess(argv, 0, canonical(value), b"")

    def test_exact_saved_plan_preserves_data_and_never_replans(self):
        gate = self.gate(self.review("destroy", self.payload))
        result = execution.apply_saved_plan(
            gate,
            runner=self.runner,
            guard=self.guard,
            receipt_path=self.folder / "receipt.json",
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual([a[1] for a in self.calls], ["state", "show", "apply", "show"])
        self.assertNotIn("plan", [a[1] for a in self.calls])
        self.assertEqual(self.calls[2][-1], self.payload["plan"])

    def test_state_drift_is_ambiguous_and_never_applied(self):
        gate = self.gate(
            self.review("destroy", {**self.payload, "current_state_sha256": "0" * 64})
        )
        with self.assertRaises(ValueError):
            execution.apply_saved_plan(
                gate,
                runner=self.runner,
                guard=self.guard,
                receipt_path=self.folder / "receipt.json",
            )
        self.assertEqual([a[1] for a in self.calls], ["state"])
        self.assertEqual(
            json.loads((self.folder / "receipt.json").read_text())["status"],
            "ambiguous",
        )

    def test_fixture_snapshot_and_purge_cannot_reach_process(self):
        for mode in ("purge-data", "retain-data"):
            p = copy.deepcopy(self.payload)
            p["mode"] = mode
            if mode == "retain-data":
                modified = copy.deepcopy(self.snapshots)
                modified["encore2"]["source"] = "mock-plan"
                path = self.put("modified.json", modified)
                p.update(snapshots=str(path), snapshots_sha256=digest(path))
            gate = self.gate(self.review("destroy", p))
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                execution.apply_saved_plan(
                    gate,
                    runner=self.runner,
                    guard=self.guard,
                    receipt_path=self.folder / "receipt.json",
                )
        self.assertEqual(self.calls, [])

    def test_false_restore_proof_or_signature_refuses_before_process(self):
        proof = json.loads(Path(self.payload["backup_evidence"]).read_text())
        proof["databases"]["postgresql"]["restore_verified"] = False
        proof["signature"] = hmac.new(
            self.issuer_key,
            canonical({k: v for k, v in proof.items() if k != "signature"}),
            hashlib.sha256,
        ).hexdigest()
        self.put("backup_evidence.json", proof)
        gate = self.gate(self.review("destroy", self.payload))
        with self.assertRaises(ValueError):
            execution.apply_saved_plan(
                gate,
                runner=self.runner,
                guard=self.guard,
                receipt_path=self.folder / "receipt.json",
            )
        self.assertEqual(self.calls, [])

    def test_apply_failure_is_not_retried_or_marked_complete(self):
        gate = self.gate(self.review("destroy", self.payload))

        def runner(argv, **kwargs):
            if argv[1] == "apply":
                self.calls.append(argv)
                raise subprocess.CalledProcessError(1, argv)
            return self.runner(argv, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError):
            execution.apply_saved_plan(
                gate,
                runner=runner,
                guard=self.guard,
                receipt_path=self.folder / "receipt.json",
            )
        self.assertEqual(sum(a[1] == "apply" for a in self.calls), 1)
        self.assertEqual(
            json.loads((self.folder / "receipt.json").read_text())["status"],
            "ambiguous",
        )


class BuiltinTerraformPlan(PrivateInputs):
    def test_real_local_saved_destroy_plan_has_only_exact_delete(self):
        folder = self.folder / "terraform-local"
        folder.mkdir()
        (folder / "main.tf").write_text(
            'terraform { required_version = "= 1.11.4" }\nresource "terraform_data" "only" { input = "local-only" }\n'
        )
        tf = ROOT / "aws-infra/foundation/.cache/toolchain/terraform"

        def command(*args):
            return subprocess.run(
                [str(tf), *args], cwd=folder, check=True, capture_output=True
            )

        command("init", "-backend=false", "-input=false")
        command("apply", "-auto-approve", "-input=false")
        saved = folder / "saved.tfplan"
        command("plan", "-destroy", "-input=false", "-out=" + str(saved))
        shown = json.loads(command("show", "-json", str(saved)).stdout)
        self.assertEqual(
            execution.validate_destroy_plan(
                shown, delete=["terraform_data.only"], retained=[]
            ),
            {"terraform_data.only"},
        )
        self.assertTrue(saved.is_file())
