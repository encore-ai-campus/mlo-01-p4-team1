"""Dormant Track2 acceptance boundaries. No app implementation or AWS calls."""

import hashlib
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit

from .operations import UnsafeTransition
from .queries import TRACK2_QUERIES, TRACK2_EXECUTOR_PORTS
from .resources import (
    SPARK_DRIVER_MEMORY, SPARK_EXECUTOR_MEMORY,
    SPARK_EXECUTOR_CORES, SPARK_APPLICATION_CORES,
)


def require(value, message):
    if not value:
        raise UnsafeTransition(message)


def identity(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value),
        "invalid runtime identity",
    )
    return value


def sha256(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
        "invalid SHA256",
    )
    return value


def writer_tables(dataset_version):
    """Dataset-scoped Delta table roots inside the Foundation lake/v1 prefix."""
    identity(dataset_version)
    return [
        f"lake/venues/{dataset_version}/v1/{layer}/"
        for layer in ("bronze", "silver", "gold")
    ]


def accept_foundation(sidecar, inventory):
    from .contracts import ROOT
    import sys
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from infra_common.track2 import foundation_binding
    result = foundation_binding(sidecar, inventory)
    require(set(result["queries"]) == set(TRACK2_QUERIES) | {
        "weather-stream", "feedback-postgresql", "feedback-analytics-batch"},
        "runtime query registry differs from Foundation bridge")
    require(result["writer_tables"] == writer_tables(result["dataset_version"]),
            "runtime lake writer paths differ from Foundation")
    return result


def validate_artifact(
    binding, *, query, entrypoint, root=Path("/opt/mlo/current/apps")
):
    """Binding must come from the protected, reviewed installation payload.

    Check all files on every admission; a boolean alone is never executable acceptance.
    The caller still admits work through the existing active-group/maintenance guard.
    """
    require(
        set(binding)
        == {
            "version",
            "accepted",
            "query",
            "entrypoint",
            "files",
            "schema_sha256",
            "writer_tables",
            "dataset_version",
            "mode",
        },
        "unknown artifact contract",
    )
    require(
        binding["version"] == "0.3.0" and binding["accepted"] is True,
        "artifact is not accepted",
    )
    require(
        query in TRACK2_QUERIES and binding["query"] == query, "artifact query mismatch"
    )
    require(binding["entrypoint"] == str(entrypoint), "artifact entrypoint mismatch")
    require(
        binding["mode"] == "enqueue-active-group", "timer must use fenced active group"
    )
    sha256(binding["schema_sha256"])
    require(
        binding["writer_tables"] == writer_tables(binding["dataset_version"]),
        "dataset-scoped single-writer table set mismatch",
    )
    root = Path(root).resolve()
    require(
        isinstance(binding["files"], dict) and 0 < len(binding["files"]) <= 128,
        "bounded artifact file manifest required",
    )
    for name, digest in binding["files"].items():
        path = Path(name)
        require(
            path.is_absolute()
            and not path.is_symlink()
            and ".." not in path.parts
            and path.resolve().is_relative_to(root),
            "artifact path escape",
        )
        sha256(digest)
        require(
            path.is_file() and path.stat().st_size <= 32 * 1024 * 1024,
            "artifact file missing or oversized",
        )
        require(
            hashlib.sha256(path.read_bytes()).hexdigest() == digest,
            "artifact checksum mismatch",
        )
    require(str(entrypoint) in binding["files"], "entrypoint missing from manifest")
    return binding


def checkpoint(environment, generation, query):
    identity(environment)
    identity(generation)
    require(query in TRACK2_QUERIES, "unregistered Track2 query")
    return f"aws/{environment}/{generation}/{query}/checkpoint/"


def proposal(environment, generation):
    return {
        "version": "0.3.0",
        "accepted": False,
        "ready_for_aws": False,
        "queries": {
            q: {
                "driver": ports[0],
                "block": ports[1],
                "executor": TRACK2_EXECUTOR_PORTS[q],
                "checkpoint": checkpoint(environment, generation, q),
                "driver_memory": SPARK_DRIVER_MEMORY,
                "executor_memory": SPARK_EXECUTOR_MEMORY,
                "executor_cores": SPARK_EXECUTOR_CORES,
                "total_executor_cores": SPARK_APPLICATION_CORES,
                "mode": "enqueue-active-group",
                "schedule_proposal": "*:0/15",
                "stop_scope": "whole active group; all old Drivers/executors confirmed stopped",
            }
            for q, ports in TRACK2_QUERIES.items()
        },
        "workers": {
            "ads-observation": {
                "accepted": False,
                "schedule_proposal": "*-*-* *:*:00",
                "mode": "accepted-read-only-projection",
                "source": "Mongo log_record preserved",
            }
        },
        "unresolved": [
            "reviewed query/job/stop registry contract",
            "accepted pipeline/app artifacts",
            "S3 bucket/prefix IAM",
            "retention/archive policy",
            "verified origin TLS binding",
        ],
    }


def validate_edge(binding):
    """Validate the explicitly selected transport profile; never infer full TLS."""
    require(binding.get("accepted") is True, "edge inputs are unresolved")
    profile = binding.get("transport_profile", "full-tls")
    require(profile in {"full-tls", "viewer-https-demo"}, "unknown edge transport profile")
    origin = binding.get("origin_hostname", "")
    viewer = urlsplit(binding.get("viewer_origin", ""))
    require(
        viewer.scheme == "https"
        and viewer.hostname
        and viewer.path in {"", "/"}
        and not viewer.query
        and not viewer.fragment
        and not viewer.username
        and viewer.port in {None, 443},
        "fixed HTTPS viewer origin required",
    )
    require(
        binding.get("rp_id") == viewer.hostname,
        "RP ID must match fixed viewer hostname",
    )
    if profile == "viewer-https-demo":
        require(
            binding.get("environment") == "dev"
            and binding.get("demo_only") is True
            and binding.get("synthetic_data_only") is True,
            "HTTP origin is limited to the explicitly accepted synthetic demo",
        )
        require(
            re.fullmatch(r"[a-z0-9-]+\.ap-northeast-2\.elb\.amazonaws\.com", origin)
            and re.fullmatch(r"[a-z0-9]+\.cloudfront\.net", viewer.hostname),
            "demo requires actual Seoul ALB and fixed CloudFront hostnames",
        )
        require(
            binding.get("origin_protocol") == "http"
            and binding.get("origin_port") == 80
            and binding.get("control_protocol") == "http"
            and binding.get("control_port") == 8000
            and binding.get("origin_certificate_verified") is False
            and binding.get("control_tls_verified") is False
            and binding.get("origin_tls_name", "") == "",
            "demo transport must accurately report its unencrypted origin and target",
        )
    else:
        require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]+[a-z0-9]", origin)
            and not origin.endswith((".amazonaws.com", ".elb.amazonaws.com")),
            "controlled origin hostname required for verified certificate",
        )
        require(
            binding.get("origin_tls_name") == origin
            and binding.get("origin_certificate_verified") is True
            and binding.get("control_tls_verified") is True,
            "full origin TLS verification required",
        )
    require(
        binding.get("secure_cookies") is True
        and binding.get("csrf_origin") == viewer.geturl().rstrip("/"),
        "secure cookie/CSRF origin mismatch",
    )
    require(
        binding.get("viewer_protocol_header") == "CloudFront-Forwarded-Proto"
        and binding.get("viewer_protocol") == "https"
        and binding.get("edge_overwrites_viewer_header") is True,
        "trusted viewer protocol marker required",
    )
    ranges = binding.get("trusted_proxy_cidrs", [])
    require(
        isinstance(ranges, list) and ranges, "restricted trusted proxy source required"
    )
    try:
        require(
            all(
                ipaddress.ip_network(cidr).is_private
                and ipaddress.ip_network(cidr).prefixlen > 0
                for cidr in ranges
            ),
            "unrestricted proxy range",
        )
    except ValueError as exc:
        raise UnsafeTransition("invalid trusted proxy CIDR") from exc
    require(
        binding.get("ingress_restricted") is True, "edge-only ingress evidence required"
    )
    return {"ready": True, "transport_profile": profile,
            "full_tls": profile == "full-tls",
            "passkey_ceremony": "not_run", "ready_for_aws": False}
