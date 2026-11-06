"""Synthetic v1 fixtures and external local-test approval authority."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from mlo_lifecycle.journal import atomic, sign
from mlo_lifecycle.model import BASE, ROOT, ROOTS, binding_scope, digest, read, runtime_facts


def fixture():
    inv = read(ROOT / "aws-infra/contracts/inventory.example.json")
    inv["generation"] = "lifecycle-local-v1"
    states = {root: {"backend": {"kind": "snapshot-only", "key": f"dev/{root}/terraform.tfstate"},
                     "lineage": "sim-lineage-" + root, "serial": 7, "resources": []} for root in ROOTS}
    deps = {}

    def add(root, address, *, node=None, identity=None, protected=False, parents=()):
        target = root + ":" + address
        states[root]["resources"].append({"address": address, "id": identity or "sim:" + target,
                                          "account": root if root.startswith("encore") else "encore1",
                                          "project": "mlo-p4", "generation": inv["generation"],
                                          "ownership": "managed", "protected": protected, "node": node})
        deps[target] = list(parents)
        return target

    vpcs = {}
    for root in inv["accounts"]:
        vpcs[root] = add(root, "aws_vpc.this", protected=True)
        add(root, "aws_vpc_endpoint.s3", parents=[vpcs[root]])
    for node in inv["nodes"]:
        root, name = node["account"], node["name"]
        add(root, f'aws_instance.host["{name}"]', node=name, identity=node["instance_id"], parents=[vpcs[root]])
        add(root, f'aws_ebs_volume.data["{name}"]', protected=True, parents=[vpcs[root]])
    for name in ("raw", "lake", "backup", "transfer"):
        add("encore1", f'aws_s3_bucket.data["{name}"]', protected=True)
    for name in ("ssm", "ssmmessages"):
        add("encore1", f'aws_vpc_endpoint.ssm["{name}"]', parents=[vpcs["encore1"]])
        add("encore1", f'aws_route53_zone.ssm["{name}"]', parents=[vpcs["encore1"]])
    for left, right in inv["peering_pairs"]:
        pair = left + "-" + right
        pcx = add("connectivity", f'aws_vpc_peering_connection.pair["{pair}"]', parents=[vpcs[left], vpcs[right]])
        for direction in (left + "-" + right, right + "-" + left):
            add("connectivity", f'aws_route.pair["{direction}"]', parents=[pcx])
    add("bootstrap", "aws_s3_bucket.state", protected=True)
    binding = {"project": "mlo-p4", "environment": inv["environment"], "generation": inv["generation"],
               "account_ids": {a: str(111111111111 + i) for i, a in enumerate(inv["accounts"])},
               "state_bindings": {r: {k: s[k] for k in ("backend", "lineage", "serial")} for r, s in states.items()},
               "targets": {r + ":" + v["address"]: v["id"] for r, s in states.items() for v in s["resources"]},
               "untracked_data": []}
    return copy.deepcopy({"schema_version": "lifecycle-input-v1", "example_only": True,
                          "inventory": inv, "binding": binding, "states": states, "dependencies": deps})


def authorization(plan, bundle, key, now=None):
    now = time.time() if now is None else now
    approval = sign({"kind": "local-approval-v1", "reviewer": "synthetic-test-authority",
                     "plan_sha256": plan["sha256"], "scope": binding_scope(bundle), "mode": plan["mode"],
                     "delete": plan["delete"], "protection_release": plan["protection_release"],
                     "purge_data": sorted(k for k in plan["delete"] if plan["targets"][k]["kind"] == "data"),
                     "issued_at": now, "expires_at": now + 300}, key)
    evidence = sign({"kind": "local-evidence-v1", "plan_sha256": plan["sha256"], "scope": binding_scope(bundle),
                     "input_sha256": digest(bundle), "runtime": runtime_facts(bundle), "facts": {"identity_verified": True,
                     "state_snapshot_verified": True, "no_external_resources": True},
                     "issued_at": now, "expires_at": now + 300}, key)
    return approval, evidence


def cli(*args, expected=0):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "TF_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, str(BASE / "scripts/lifecycle.py"), *map(str, args)],
                            capture_output=True, text=True, env=env, timeout=90)
    if result.returncode != expected:
        raise AssertionError(f"CLI code {result.returncode}, expected {expected}: {result.stderr[-3000:]}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def prepare(folder, mode="retain-data"):
    folder = Path(folder)
    bundle = fixture()
    bundle_path, plan_path, lab = folder / "bundle.json", folder / "plan.json", folder / "lab"
    atomic(bundle_path, bundle)
    plan = cli("plan", "--bundle", bundle_path, "--mode", mode, "--output", plan_path)
    cli("lab-init", "--bundle", bundle_path, "--lab", lab)
    key = os.urandom(32)
    (folder / "authority.key").write_bytes(key)
    (folder / "authority.key").chmod(0o600)
    approval, evidence = authorization(plan, bundle, key)
    atomic(folder / "approval.json", approval)
    atomic(folder / "evidence.json", evidence)
    args = ["--bundle", bundle_path, "--plan", plan_path, "--lab", lab,
            "--approval", folder / "approval.json", "--evidence", folder / "evidence.json",
            "--authority-key", folder / "authority.key"]
    return bundle, plan, key, args
