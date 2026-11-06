"""Canonical inputs, identities and fail-closed local target validation."""

import hashlib
import json
import math
from pathlib import Path
import re
import sys

BASE = Path(__file__).resolve().parents[2]
ROOT = BASE.parents[1]
ACCOUNTS = tuple("encore" + str(n) for n in range(1, 6))
ROOTS = (*ACCOUNTS, "connectivity", "bootstrap")
KINDS = {
    "aws_instance": "compute",
    "aws_ebs_volume": "data",
    "aws_s3_bucket": "data",
    "aws_vpc": "network",
    "aws_vpc_endpoint": "access",
    "aws_route53_zone": "access",
    "aws_vpc_peering_connection": "connectivity",
    "aws_route": "connectivity",
}


class Refused(ValueError):
    def __init__(
        self, reason, *, code="invalid_input", phase="validation", step_id=None
    ):
        super().__init__(reason)
        self.code, self.phase, self.step_id = code, phase, step_id


def need(condition, reason, **context):
    if not condition:
        raise Refused(reason, **context)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def local_path(path):
    path = Path(path).absolute()
    # macOS system temp aliases are fixed host prefixes, not caller-controlled links.
    for alias, target in (
        (Path("/var"), Path("/private/var")),
        (Path("/tmp"), Path("/private/tmp")),
    ):
        if (
            path.is_relative_to(alias)
            and alias.is_symlink()
            and alias.resolve() == target
        ):
            path = target / path.relative_to(alias)
    need(
        not any(p.is_symlink() for p in (path, *path.parents)),
        "symlink file/ancestor refused",
        code="artifact_drift",
    )
    return path


def file_hash(path):
    path = local_path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def read(path):
    path = local_path(path)
    need(
        not path.is_symlink() and path.stat().st_size <= 8 * 1024 * 1024,
        "unsafe input file",
    )
    return json.loads(
        path.read_text(),
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(Refused("non-finite JSON")),
    )


def keys(value, expected):
    need(
        isinstance(value, dict) and set(value) == set(expected.split()),
        "unexpected/missing fields",
    )


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value)


def code_hash():
    # Bind executable code and the read-only contracts used to interpret inputs.
    files = (
        list((BASE / "src").rglob("*.py"))
        + list((BASE / "scripts").glob("*.py"))
        + [
            BASE / "requirements.lock.txt",
            BASE / "toolchain.lock.json",
            ROOT / "scripts/check_preparation.py",
            ROOT / "aws-infra/contracts/inventory.schema.json",
            ROOT / "aws-infra/contracts/topology.v1.json",
            ROOT / "aws-infra/runtime/src/mlo_runtime/operations.py",
            ROOT / "aws-infra/runtime/src/mlo_runtime/stateful.py",
            ROOT / "aws-infra/foundation/scripts/run_foundation.py",
            ROOT / "aws-infra/foundation/scripts/build_config.py",
            ROOT / "aws-infra/foundation/scripts/model.py",
        ]
    )
    files.extend((ROOT / "aws-infra/foundation/terraform").rglob("main.tf.json"))
    files.extend((ROOT / "aws-infra/contracts/infra_common").glob("*.py"))
    files.extend((ROOT / "aws-infra/runtime/src/mlo_runtime").glob("*.py"))
    files.extend(
        [
            ROOT / "aws-infra/foundation/scripts/lifecycle_export.py",
            ROOT / "aws-infra/runtime/src/mlo_runtime/database_stop.py",
        ]
    )
    return digest({str(p.relative_to(ROOT)): file_hash(p) for p in sorted(files)})


def validate_bundle(bundle):
    keys(bundle, "schema_version example_only inventory binding states dependencies")
    need(
        bundle["schema_version"] == "lifecycle-input-v1"
        and bundle["example_only"] is True,
        "only synthetic snapshots are accepted",
    )
    inv, binding, states = bundle["inventory"], bundle["binding"], bundle["states"]
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_preparation import validate_inventory

    validate_inventory(inv)
    need(inv["example_only"] is True, "real inventory disabled")
    keys(
        binding,
        "project environment generation account_ids state_bindings targets untracked_data",
    )
    need(
        binding["project"] == "mlo-p4"
        and binding["environment"] == inv["environment"] == "dev"
        and binding["generation"] == inv["generation"],
        "target environment/generation mismatch",
    )
    # Explicitly reserve these accounts for the simulator. Real identities cannot be smuggled into fixtures.
    expected_accounts = {a: str(111111111111 + i) for i, a in enumerate(ACCOUNTS)}
    need(
        binding["account_ids"] == expected_accounts,
        "synthetic five-account binding mismatch",
    )
    need(
        set(states) == set(ROOTS) == set(binding["state_bindings"]),
        "all seven state roots required",
    )
    resources = {}
    for root, state in states.items():
        keys(state, "backend lineage serial resources")
        keys(state["backend"], "kind key")
        expected_key = f"dev/{root}/terraform.tfstate"
        need(
            state["backend"] == {"kind": "snapshot-only", "key": expected_key},
            "real/wrong backend refused",
        )
        need(
            state["lineage"] == "sim-lineage-" + root
            and type(state["serial"]) is int
            and state["serial"] >= 0,
            "invalid state lineage/serial",
        )
        need(
            binding["state_bindings"][root]
            == {k: state[k] for k in ("backend", "lineage", "serial")},
            "state version/binding mismatch",
        )
        need(
            isinstance(state["resources"], list) and state["resources"],
            "empty state root",
        )
        for item in state["resources"]:
            keys(item, "address id account project generation ownership protected node")
            address = item["address"]
            need(
                isinstance(address, str)
                and re.fullmatch(
                    r'aws_[a-z0-9_]+\.[a-z0-9_]+(?:\["[a-z0-9-]+"\])?', address
                ),
                "invalid managed address",
            )
            kind = KINDS.get(address.split(".")[0])
            need(kind is not None, "unclassified resource refused")
            if root == "bootstrap":
                need(
                    address == "aws_s3_bucket.state",
                    "bootstrap resource outside local model",
                )
                kind = "backend"
            else:
                need(
                    address != "aws_s3_bucket.state",
                    "backend resource cannot move to a data root",
                )
            target = root + ":" + address
            need(target not in resources, "duplicate state target")
            need(
                item["project"] == binding["project"]
                and item["generation"] == inv["generation"]
                and item["ownership"] == "managed",
                "unknown/external resource ownership",
            )
            need(
                item["account"] == (root if root in ACCOUNTS else "encore1"),
                "resource account mismatch",
            )
            need(type(item["protected"]) is bool, "invalid protection flag")
            # This policy is part of the approved simulator code, not caller-controlled state.
            # Real configuration/API protection attestation is deliberately unsupported.
            expected_protection = kind in {"data", "network", "backend"}
            need(
                item["protected"] is expected_protection,
                "synthetic protection policy mismatch",
                code="protection_mismatch",
            )
            if kind == "compute":
                nodes = {n["name"]: n for n in inv["nodes"]}
                node = nodes.get(item["node"])
                need(
                    node is not None
                    and node["instance_id"] == item["id"]
                    and node["account"] == root
                    and address == f'aws_instance.host["{item["node"]}"]',
                    "inventory address/ID mismatch",
                )
            else:
                need(
                    item["id"] == "sim:" + target and item["node"] is None,
                    "non-synthetic resource ID",
                )
            resources[target] = {**item, "root": root, "kind": kind}
    need(
        len({r["id"] for r in resources.values()}) == len(resources),
        "duplicate resource ID",
    )
    need(
        {r["node"] for r in resources.values() if r["kind"] == "compute"}
        == {n["name"] for n in inv["nodes"]},
        "missing inventory target",
    )
    need(
        binding["targets"] == {k: v["id"] for k, v in resources.items()},
        "target set/address/ID mismatch",
    )
    deps = bundle["dependencies"]
    need(set(deps) == set(resources), "dependency target set incomplete")
    for target, parents in deps.items():
        need(
            isinstance(parents, list)
            and len(parents) == len(set(parents))
            and set(parents) <= set(resources)
            and target not in parents,
            "missing/duplicate/self dependency",
        )
    need(isinstance(binding["untracked_data"], list), "untracked-data report required")
    return resources


def binding_scope(bundle):
    return {
        k: bundle["binding"][k]
        for k in (
            "project",
            "environment",
            "generation",
            "account_ids",
            "state_bindings",
        )
    }


def runtime_facts(bundle):
    """Required synthetic observations. Never proof of remote service state."""
    return {
        "issuer": "synthetic-observer-v1",
        "generation": bundle["binding"]["generation"],
        "instances": {
            n["name"]: n["instance_id"] for n in bundle["inventory"]["nodes"]
        },
        "services": {
            name: True
            for name in (
                "control_a_fenced",
                "control_b_fenced",
                "web_admission_closed",
                "collector_stopped",
                "timers_disabled",
                "supervisor_fenced",
                "weather_stream_stopped",
                "feedback_postgresql_stopped",
                "feedback_analytics_batch_stopped",
                "kafka_producers_flushed",
                "kafka_consumers_stopped",
                "spark_drivers_absent",
                "spark_executors_absent",
                "spark_masters_idle",
                "spark_workers_idle",
                "delta_checkpoint_flushed",
                "checkpoint_objects_verified",
            )
        },
    }
