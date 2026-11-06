"""Read-only full Foundation snapshot export and removal-plan acceptance.

Reads supplied Terraform JSON/files only. Never invokes Terraform, AWS or backend.
Mock plan evidence stays explicitly different from observed state evidence.
"""

import hashlib
import json
from pathlib import Path
import re
import time

from model import FOUNDATION, ROOT, ACCOUNTS, read_json, require

KINDS = {
    **dict.fromkeys(["aws_instance"], "compute"),
    **dict.fromkeys(["aws_ebs_volume", "aws_s3_bucket"], "data"),
    **dict.fromkeys(
        [
            "aws_vpc",
            "aws_subnet",
            "aws_network_interface",
            "aws_internet_gateway",
            "aws_route_table",
            "aws_route_table_association",
            "aws_default_security_group",
            "aws_security_group",
            "aws_security_group_rule",
        ],
        "network",
    ),
    **dict.fromkeys(
        [
            "aws_route",
            "aws_vpc_peering_connection",
            "aws_vpc_peering_connection_accepter",
        ],
        "connectivity",
    ),
    **dict.fromkeys(
        [
            "aws_vpc_endpoint",
            "aws_route53_zone",
            "aws_route53_record",
            "aws_route53_vpc_association_authorization",
            "aws_route53_zone_association",
            "aws_iam_role",
            "aws_iam_role_policy",
            "aws_iam_instance_profile",
            "aws_iam_openid_connect_provider",
            "aws_ssm_document",
        ],
        "access",
    ),
    **dict.fromkeys(
        [
            "aws_lb",
            "aws_lb_listener",
            "aws_lb_listener_rule",
            "aws_lb_target_group",
            "aws_lb_target_group_attachment",
            "aws_cloudfront_distribution",
            "aws_cloudfront_cache_policy",
            "aws_cloudfront_origin_request_policy",
        ],
        "edge",
    ),
    **dict.fromkeys(
        [
            "aws_s3_bucket_ownership_controls",
            "aws_s3_bucket_policy",
            "aws_s3_bucket_public_access_block",
            "aws_s3_bucket_server_side_encryption_configuration",
            "aws_s3_bucket_versioning",
        ],
        "data-control",
    ),
    "aws_volume_attachment": "attachment",
}


def sha(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def unindex(address):
    return re.sub(r'\[(?:"(?:[^"\\]|\\.)*"|[0-9]+)\]', "", address)


def configurations(root, prefix="", seen=()):
    root = Path(root).resolve()
    require(
        root.is_relative_to(FOUNDATION / "terraform") and root not in seen,
        "unsafe/cyclic module source",
    )
    result = {}
    for file in sorted(root.glob("*.tf.json")):
        value = read_json(file)
        for kind, entries in value.get("resource", {}).items():
            for name, body in entries.items():
                result[prefix + kind + "." + name] = body
        for name, module in value.get("module", {}).items():
            require(
                module.get("source", "").startswith("."),
                "unreviewed remote module source",
            )
            result.update(
                configurations(
                    root / module["source"],
                    prefix + "module." + name + ".",
                    (*seen, root),
                )
            )
    return result


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def export_snapshot(root, shown, metadata, *, raw_state=None, source="state"):
    require(root in [*ACCOUNTS, "connectivity", "bootstrap"], "unknown state root")
    require(source in {"state", "mock-plan"}, "unsupported snapshot source")
    require(
        set(metadata)
        == {
            "environment",
            "generation",
            "account",
            "region",
            "state_key",
            "lineage",
            "serial",
            "expected_targets",
            "observed_at",
        },
        "missing snapshot binding",
    )
    require(
        metadata["environment"] == "dev"
        and metadata["region"] == "ap-northeast-2"
        and metadata["account"] == root
        and metadata["state_key"] == f"dev/{root}/terraform.tfstate"
        and type(metadata["serial"]) is int
        and metadata["serial"] >= 0
        and metadata["generation"]
        and metadata["lineage"]
        and type(metadata["observed_at"]) in (float, int)
        and 0 <= time.time() - metadata["observed_at"] <= 300,
        "stale/mixed snapshot metadata",
    )
    config_root = (
        FOUNDATION
        / "terraform"
        / ("bootstrap" if root == "bootstrap" else f"environments/dev/{root}")
    )
    configs = configurations(config_root)
    if source == "mock-plan":
        changes = [
            r
            for r in shown["resource_changes"]
            if r["mode"] == "managed" and r["change"]["after"] is not None
        ]
        items = [dict(r, values=r["change"]["after"]) for r in changes]
    else:
        require(
            shown.get("format_version", "1.0").split(".")[0] == "1",
            "unsupported Terraform JSON version",
        )

        def walk(module):
            return module.get("resources", []) + [
                r for child in module.get("child_modules", []) for r in walk(child)
            ]

        items = [
            r for r in walk(shown["values"]["root_module"]) if r["mode"] == "managed"
        ]
        require(
            raw_state is not None
            and raw_state["lineage"] == metadata["lineage"]
            and raw_state["serial"] == metadata["serial"],
            "raw state lineage/serial required",
        )
        actual = {}
        for resource in raw_state["resources"]:
            if resource["mode"] != "managed":
                continue
            base = (
                (resource.get("module", "") + "." if resource.get("module") else "")
                + resource["type"]
                + "."
                + resource["name"]
            )
            for instance in resource["instances"]:
                require(
                    "deposed" not in instance, "deposed instance must be reconciled"
                )
                address = base + (
                    "[" + json.dumps(instance["index_key"]) + "]"
                    if "index_key" in instance
                    else ""
                )
                require(address not in actual, "duplicate raw state address")
                actual[address] = instance["attributes"].get("id")
        require(
            actual == {r["address"]: r["values"].get("id") for r in items},
            "raw/show state set/ID inequality",
        )
    resources, unknown = {}, []
    for item in items:
        address = item["address"]
        require(
            address not in resources and isinstance(address, str),
            "duplicate resource address",
        )
        config = configs.get(unindex(address))
        kind = KINDS.get(item["type"])
        if config is None or kind is None:
            unknown.append(address)
        values = item["values"]
        identity = values.get("id")
        if source == "state":
            require(
                isinstance(identity, str) and identity,
                "unknown resource ID in observed state",
            )
        protected = bool(
            (config or {}).get("lifecycle", {}).get("prevent_destroy", False)
        )
        api_protection = values.get("enable_deletion_protection", False)
        require(type(api_protection) is bool, "unknown API deletion protection")
        if item["type"] == "aws_lb":
            require(
                api_protection == config.get("enable_deletion_protection"),
                "configuration/API protection mismatch",
            )
        resources[address] = {
            "address": address,
            "module_address": item.get("module_address", ""),
            "provider": item.get("provider_name"),
            "type": item["type"],
            "id": identity,
            "kind": "backend" if root == "bootstrap" else kind,
            "protected": protected or api_protection,
            "configuration": config,
            "dependencies": [],
            "ownership": "snapshot-bound",
            **metadata,
        }
    require(
        metadata["expected_targets"] == {k: r["id"] for k, r in resources.items()},
        "expected target set/ID mismatch",
    )
    # Resolve explicit resource expressions conservatively across count/for_each instances.
    # Exact evaluation and implicit provider edges are checked again in a saved removal plan.
    for address, item in resources.items():
        config = item["configuration"] or {}
        module = unindex(address).rsplit(item["type"] + ".", 1)[0]
        references = set()
        for value in strings(config):
            for match in re.findall(r"aws_[a-z0-9_]+\.[a-zA-Z0-9_]+", value):
                references.update(
                    k
                    for k in resources
                    if unindex(k) == module + match and k != address
                )
        item["dependencies"] = sorted(references)
        # Do not copy all targets into every resource record.
        item.pop("expected_targets")
    body = {
        "schema_version": "foundation-snapshot-v1",
        "source": source,
        "root": root,
        "binding": metadata,
        "resources": resources,
        "unknown": sorted(unknown),
        "configuration_sha256": sha(configs),
        "source_sha256": sha(shown),
        "raw_state_sha256": sha(raw_state) if raw_state is not None else None,
        "ready_for_aws": False,
        "execution_enabled": False,
        "dependency_scope": "configuration expressions; provider implicit edges require reviewed saved plan",
    }
    return {**body, "sha256": sha(body)}


def audit_removal(
    snapshot, candidate, *, delete, retain, protection_release, saved_plan_sha256
):
    body = {k: v for k, v in snapshot.items() if k != "sha256"}
    require(
        sha(body) == snapshot["sha256"] and not snapshot["unknown"],
        "snapshot drift/unknown resources",
    )
    require(
        0 <= time.time() - snapshot["binding"]["observed_at"] <= 300,
        "stale removal snapshot",
    )
    resources = snapshot["resources"]
    require(
        not set(delete) & set(retain) and set(delete) | set(retain) == set(resources),
        "incomplete partition",
    )
    require(
        not any(resources[k]["kind"] == "backend" for k in delete),
        "backend retirement is outside scope",
    )
    expected_protection = sorted(k for k in delete if resources[k]["protected"])
    require(
        sorted(protection_release) == expected_protection
        and re.fullmatch(r"[a-f0-9]{64}", saved_plan_sha256),
        "exact configuration/protection review required",
    )
    changes = [r for r in candidate["resource_changes"] if r["mode"] == "managed"]
    actions = {r["address"]: r["change"]["actions"] for r in changes}
    require(len(changes) == len(actions), "duplicate removal plan address")
    require(
        set(actions) == set(resources), "saved plan managed set differs from snapshot"
    )
    require(
        all(
            actions[k] == (["delete"] if k in delete else ["no-op"]) for k in resources
        ),
        "replacement/create/update/undeclared delete is outside approval",
    )
    require(
        all(
            r["change"].get("before", {}).get("id") == resources[r["address"]]["id"]
            for r in changes
        ),
        "removal plan resource identity differs from snapshot",
    )
    require(candidate.get("errored") is not True, "errored removal plan")
    return {
        "accepted": True,
        "ready_for_aws": False,
        "execution_enabled": False,
        "snapshot_sha256": snapshot["sha256"],
        "saved_plan_sha256": saved_plan_sha256,
        "plan_json_sha256": sha(candidate),
        "delete": sorted(delete),
        "retain": sorted(retain),
        "protection_release": expected_protection,
    }
