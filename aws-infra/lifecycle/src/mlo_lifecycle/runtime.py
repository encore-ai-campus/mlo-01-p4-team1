"""C -> B engine stop integration with common operation and shutdown evidence."""

import sys
from pathlib import Path
from .model import ROOT, need, digest, read
from .journal import atomic

sys.path[:0] = [str(ROOT / "aws-infra/contracts"), str(ROOT / "aws-infra/runtime/src")]
from infra_common.coordination import require_operation
from infra_common.evidence import validate_shutdown
from mlo_runtime.operations import stop_stateful


def stop_engine_backends(inventory, backends, guard, evidence, issuers, directory):
    need(set(backends) == {"postgresql", "mongodb"}, "both engine backends required")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def current_evidence():
        token = require_operation(guard, "destroy")
        proof = evidence() if callable(evidence) else evidence
        validate_shutdown(proof, inventory, token, issuers)
        artifact = directory / "evidence" / (digest(proof) + ".json")
        if not artifact.exists():
            atomic(artifact, proof)
        return proof

    proof = current_evidence()
    token = require_operation(guard, "destroy")
    journal = directory / "engine-shutdown.json"
    need(
        not journal.exists(),
        "existing engine shutdown requires independent reconciliation",
    )
    result = {
        "schema_version": "engine-shutdown-v1",
        "ready_for_aws": False,
        "operation_id": token["operation_id"],
        "epoch": token["epoch"],
        "generation": inventory["generation"],
        "evidence_sha256": digest(proof),
        "completed": {},
        "pending": None,
        "complete": False,
    }
    atomic(journal, result)
    for node, backend in backends.items():
        token = require_operation(guard, "destroy")
        current_evidence()
        need(
            backend.binding["node"] == node
            and backend.binding["generation"] == inventory["generation"],
            "engine binding mismatch",
        )
        if not inventory["example_only"]:
            expected = next(
                n["instance_id"] for n in inventory["nodes"] if n["name"] == node
            )
            need(
                backend.binding.get("instance_id") == expected,
                "engine host differs from shutdown observations",
            )
        result["pending"] = node
        atomic(journal, result)
        backend.shutdown_check = current_evidence
        receipt = stop_stateful(
            node, backend, receipt_path=directory / (node + ".receipt.json")
        )
        need(
            receipt["phase"] == "complete" and backend.observe_stopped(node),
            "engine shutdown unconfirmed",
        )
        result["completed"][node] = digest(receipt)
        result["pending"] = None
        atomic(journal, result)
    require_operation(guard, "destroy")
    result["complete"] = True
    atomic(journal, result)
    return result
