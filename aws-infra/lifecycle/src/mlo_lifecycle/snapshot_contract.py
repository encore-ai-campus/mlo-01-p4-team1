"""Audit an explicit offline export envelope; never read live Terraform/AWS state.

The future Foundation exporter owns extraction from raw state/show/config/schema.
This consumer validates mock normalized evidence and cannot enable execution.
"""

import re

from .model import KINDS, digest, keys, need
from .planner import order

SEGMENT = r'[a-zA-Z_][a-zA-Z0-9_]*(?:\[(?:[0-9]+|"[a-zA-Z0-9_-]+")\])?'
MODULE = rf'(?:module\.{SEGMENT}\.)*'
ADDRESS = re.compile(rf'{MODULE}aws_[a-z0-9_]+\.{SEGMENT}')


def audit_export(envelope):
    keys(envelope, "schema_version example_only binding state configuration provider_schemas expected_targets")
    need(envelope["schema_version"] == "offline-foundation-export-v1" and envelope["example_only"] is True,
         "only explicit offline mock exports accepted")
    binding, state = envelope["binding"], envelope["state"]
    keys(binding, "project environment generation account region state_key lineage serial")
    need(binding["project"] == "mlo-p4" and binding["environment"] == "dev"
         and binding["account"] in {str(111111111111 + i) for i in range(5)}
         and binding["region"] == "ap-northeast-2" and binding["generation"]
         and binding["state_key"].startswith("dev/") and type(binding["serial"]) is int,
         "invalid/sensitive mock binding")
    keys(state, "lineage serial resources")
    need(state["lineage"] == binding["lineage"] and state["serial"] == binding["serial"],
         "state snapshot is stale", code="state_drift")
    configuration, schemas = envelope["configuration"], envelope["provider_schemas"]
    resources, blockers, unknown = {}, [], []
    for item in state["resources"]:
        keys(item, "address module_address type provider id account region state_key lineage serial ownership dependencies protected")
        address = item["address"]
        need(isinstance(address, str) and ADDRESS.fullmatch(address), "invalid expanded managed address")
        need(address not in resources, "duplicate expanded resource")
        prefix = address[:address.rfind(item["type"] + ".")].rstrip(".")
        need(item["module_address"] == prefix, "module address mismatch")
        need(all(item[k] == binding[k] for k in ("account", "region", "state_key", "lineage", "serial"))
             and item["ownership"] == "managed" and item["id"].startswith("sim:"), "ownership/binding mismatch")
        need(type(item["protected"]) is bool, "missing/unknown protection")
        config = configuration.get(address)
        schema = schemas.get(item["provider"], {})
        if item["type"] not in KINDS or item["type"] not in schema or config is None:
            unknown.append(address)
        else:
            keys(config, "references prevent_destroy deletion_protection observed_protection")
            need(all(type(config[k]) is bool for k in ("prevent_destroy", "deletion_protection", "observed_protection")),
                 "unknown protection attestation")
            expected = config["prevent_destroy"] or config["deletion_protection"]
            need(item["protected"] is expected and config["observed_protection"] is config["deletion_protection"],
                 "configuration/API protection conflict", code="protection_mismatch")
            need(set(config["references"]) <= set(item["dependencies"]), "configuration references omitted from DAG")
        resources[address] = item
    need({k: v["id"] for k, v in resources.items()} == envelope["expected_targets"], "full target set mismatch")
    graph = {k: v["dependencies"] for k, v in resources.items()}
    order(graph)
    if unknown:
        blockers.append("unknown resource/configuration/provider schema")
    blockers.append("live exporter/provenance and real resource graph not accepted")
    return {"schema_version": "offline-foundation-audit-v1", "ready_for_aws": False,
            "execution_enabled": False, "source_sha256": digest(envelope), "unknown": sorted(unknown),
            "blockers": blockers, "candidate_targets": sorted(resources),
            "before_set": sorted(resources), "graph": graph,
            "provenance": "caller-supplied mock evidence; not authoritative live state"}


def check_partition(before, deleted, retained, after):
    """Whole-set conservation; unknown/unowned leftovers must never disappear silently."""
    need(len(before) == len(set(before)) and len(deleted) == len(set(deleted))
         and len(retained) == len(set(retained)) and len(after) == len(set(after)), "duplicate target set")
    need(not set(deleted) & set(retained) and set(before) == set(deleted) | set(retained)
         and set(after) == set(retained), "delete/retain/observed set inequality", code="state_drift")
