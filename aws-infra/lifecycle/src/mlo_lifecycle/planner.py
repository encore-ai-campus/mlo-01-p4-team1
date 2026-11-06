"""Build a shutdown DAG independently from resource creation/deletion order."""

import time

from .model import binding_scope, code_hash, digest, need, timestamp, validate_bundle


def order(graph):
    remaining = {k: set(v) for k, v in graph.items()}
    need(all(v <= set(graph) for v in remaining.values()), "missing DAG dependency")
    result = []
    while remaining:
        ready = sorted(k for k, v in remaining.items() if not v)
        need(bool(ready), "dependency cycle")
        result.extend(ready)
        for k in ready:
            del remaining[k]
        for parents in remaining.values():
            parents.difference_update(ready)
    return result


def plan(bundle, mode="retain-data", *, now=None, ttl=3600):
    resources = validate_bundle(bundle)
    order(bundle["dependencies"])
    need(mode in {"retain-data", "purge-data"}, "unsupported data policy")
    now = time.time() if now is None else now
    need(timestamp(now) and timestamp(ttl) and ttl == int(ttl) and 0 < ttl <= 3600, "invalid plan lifetime")
    retained = {k for k, r in resources.items() if r["kind"] == "backend"
                or (mode == "retain-data" and r["kind"] == "data")}
    # A retained data volume may still require its VPC. Preserve the entire dependency closure.
    pending = list(retained)
    while pending:
        for dependency in bundle["dependencies"][pending.pop()]:
            if dependency not in retained:
                retained.add(dependency)
                pending.append(dependency)
    deleted = set(resources) - retained
    steps = {
        "freeze": {"action": "freeze", "target": None, "requires": []},
        "idle": {"action": "idle", "target": None, "requires": ["freeze"]},
        "stateful:postgresql": {"action": "stateful", "target": "postgresql", "requires": ["idle"]},
        "stateful:mongodb": {"action": "stateful", "target": "mongodb", "requires": ["idle"]},
    }
    shutdown = ["stateful:postgresql", "stateful:mongodb"]
    if mode == "purge-data":
        for target in sorted(deleted):
            if resources[target]["kind"] == "data":
                steps["backup-data:" + target] = {"action": "backup-data", "target": target,
                                                "requires": list(shutdown)}
    backups = [key for key in steps if key.startswith("backup-data:")]
    # Consumers must disappear before the provider they depend on.
    for target in sorted(deleted):
        item = resources[target]
        consumers = ["delete:" + consumer for consumer in deleted
                     if target in bundle["dependencies"][consumer]]
        # Every destructive consumer keeps source, access and communication providers alive
        # until every backup/restore proof is durable. This holds for any topological schedule.
        requires = set(consumers + shutdown + backups)
        if item["kind"] == "data":
            requires.add("backup-data:" + target)
        # Connectivity/access remain until every compute consumer has finished, even if TF omitted that edge.
        if item["kind"] in {"connectivity", "access", "network"}:
            requires.update("delete:" + k for k in deleted if resources[k]["kind"] == "compute")
        steps["delete:" + target] = {"action": "delete", "target": target, "requires": sorted(requires)}
    sequence = order({k: v["requires"] for k, v in steps.items()})
    body = {
        "schema_version": "lifecycle-plan-v1", "example_only": True, "ready_for_aws": False,
        "adapter": "local-simulator", "mode": mode, "created_at": now, "expires_at": now + ttl,
        "scope": binding_scope(bundle), "input_sha256": digest(bundle), "code_sha256": code_hash(),
        "targets": resources, "delete": sorted(deleted), "retain": sorted(retained),
        "protection_release": sorted(k for k in deleted if resources[k]["protected"]),
        "blockers": ["unmigrated root/container data"] if bundle["binding"]["untracked_data"] else [],
        "untracked_data": bundle["binding"]["untracked_data"],
        "steps": [{"id": key, **steps[key]} for key in sequence],
        "cost_may_remain": bool(retained), "removal_semantics": "synthetic-logical-removal",
        "aws_blockers": ["Foundation destroy/protection contract", "remote Runtime hooks",
                         "shared A/B/C execution lock", "real identity/state/backup acceptance"],
    }
    return {**body, "sha256": digest(body)}


def validate_plan(value, bundle, *, now=None, reporting=False):
    now = time.time() if now is None else now
    expected = plan(bundle, value["mode"], now=value["created_at"],
                    ttl=value["expires_at"] - value["created_at"])
    need(value == expected, "plan/hash/code/state/policy changed; replan required", code="state_drift")
    if not reporting:
        need(value["created_at"] <= now < value["expires_at"], "plan expired/not yet valid", code="plan_expired")
        need(not value["blockers"], "plan has unresolved data-loss blockers")
    return value
