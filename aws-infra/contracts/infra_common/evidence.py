"""Scoped per-node shutdown evidence; an assertion is not live observation proof.

Trusted issuers are supplied separately from evidence. Lab provenance cannot be
used with a non-example inventory. No evidence/approval is minted by the verifier.
"""

import hashlib
import hmac
import math
import time
from .coordination import canonical, require

SERVICES = {
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
}


def validate_shutdown(evidence, inventory, token, issuers, *, now=None):
    now = time.time() if now is None else now
    require(
        set(evidence)
        == {
            "schema_version",
            "source",
            "operation_id",
            "epoch",
            "generation",
            "nodes",
            "services",
            "issuer",
            "observed_at",
            "expires_at",
            "signature",
        },
        "incomplete shutdown evidence",
    )
    require(
        evidence["schema_version"] == "runtime-shutdown-evidence-v1"
        and evidence["operation_id"] == token["operation_id"]
        and evidence["epoch"] == token["epoch"]
        and type(evidence["epoch"]) is int
        and evidence["generation"]
        == inventory["generation"]
        == token["scope"]["generation"],
        "wrong shutdown operation/epoch/generation",
    )
    require(
        evidence["source"]
        == ("local-lab" if inventory["example_only"] else "host-observations"),
        "lab/real evidence mixing",
    )
    issuer = evidence["issuer"]
    require(
        isinstance(issuer, str) and issuer in issuers and len(issuers[issuer]) >= 32,
        "untrusted shutdown issuer",
    )
    body = {k: v for k, v in evidence.items() if k != "signature"}
    signature = hmac.new(issuers[issuer], canonical(body), hashlib.sha256).hexdigest()
    require(
        isinstance(evidence["signature"], str)
        and hmac.compare_digest(signature, evidence["signature"]),
        "shutdown signature invalid",
    )
    require(
        all(
            type(evidence[k]) in (int, float) and math.isfinite(evidence[k])
            for k in ("observed_at", "expires_at")
        )
        and evidence["observed_at"] <= now < evidence["expires_at"]
        and now - evidence["observed_at"] <= 30
        and evidence["expires_at"] - evidence["observed_at"] <= 30,
        "shutdown evidence stale",
    )
    require(
        evidence["nodes"]
        == {
            n["name"]: {
                "instance_id": n["instance_id"],
                "generation": inventory["generation"],
            }
            for n in inventory["nodes"]
        },
        "missing/mixed node identity observations",
    )
    require(
        set(evidence["services"]) == SERVICES
        and all(v is True for v in evidence["services"].values()),
        "runtime admission/idle/flush evidence missing",
    )
    return body
