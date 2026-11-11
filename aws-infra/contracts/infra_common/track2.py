"""Cross-role Track2 admission. Validated fixture inputs never attest AWS readiness."""
import hashlib
import json
import re
import uuid

QUERY = "venues-catalog"
LEGACY = ("weather-stream", "feedback-postgresql", "feedback-analytics-batch")


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def foundation_binding(sidecar, inventory):
    """Consume A's evaluated sidecar, bind it to the exact common inventory."""
    require(set(sidecar) == set("schema_version environment generation example_only region inventory_sha256 enabled dataset_version driver_queries ingest_nodes media_bucket_ref archive_bucket_ref distribution_arn tls_status ready_for_aws paths".split()),
            "unknown or missing Foundation Track2 field")
    require(sidecar["schema_version"] == "foundation-track2/1"
            and sidecar["enabled"] is True and sidecar["ready_for_aws"] is False,
            "enabled versioned Foundation sidecar required")
    require(sidecar["inventory_sha256"] == digest(inventory), "inventory hash mismatch")
    require(all(type(sidecar[k]) is type(inventory[k]) and sidecar[k] == inventory[k]
                for k in ("environment", "generation", "example_only", "region")),
            "mixed generation, environment or fixture scope")
    dataset = sidecar["dataset_version"]
    require(isinstance(dataset, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", dataset),
            "invalid dataset version")
    nodes = {n["name"]: n for n in inventory["nodes"]}
    owners = sidecar["driver_queries"]
    require(isinstance(owners, dict) and owners and set(owners) <= {"control-a", "control-b"},
            "Control query owner required")
    require(all(qs == [QUERY] and nodes[node]["role"] == "control"
                for node, qs in owners.items()), "unregistered query or owner")
    require(isinstance(sidecar["ingest_nodes"], list)
            and len(set(sidecar["ingest_nodes"])) == len(sidecar["ingest_nodes"])
            and set(sidecar["ingest_nodes"]) <= {"control-a", "control-b"}, "invalid ingest registry")
    env, gen = inventory["environment"], inventory["generation"]
    expected = {
        "raw_venues": f"raw/venues/{dataset}/",
        "lake_venues": f"lake/venues/{dataset}/v1/",
        "checkpoints": {node: [f"aws/{env}/{gen}/{QUERY}/checkpoint/"] for node in owners},
        "approved_photos": "media/venues/approved/" if sidecar["media_bucket_ref"] else None,
        "archive": f"observability/{env}/{gen}/" if sidecar["archive_bucket_ref"] else None,
    }
    require(sidecar["paths"] == expected, "producer/consumer storage path mismatch")
    require(sidecar["tls_status"] in {"CONFIGURED_NOT_VERIFIED", "UNRESOLVED_ORIGIN_NAME_CERTIFICATE", "VIEWER_HTTPS_ORIGIN_HTTP"},
            "TLS configuration is not verification")
    for name in ("media_bucket_ref", "archive_bucket_ref"):
        bucket = sidecar[name]
        require(bucket is None or isinstance(bucket, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket), "invalid optional bucket")
        if bucket:
            require(bucket.startswith("example-only-") == inventory["example_only"], "mixed fixture storage")
    return {"version": "track2-bridge/1", "foundation_sha256": digest(sidecar),
            "inventory_sha256": digest(inventory), "environment": env, "generation": gen,
            "dataset_version": dataset, "queries": [*LEGACY, QUERY],
            "writer_tables": [expected["lake_venues"] + layer + "/" for layer in ("bronze", "silver", "gold")],
            "query_owners": owners, "checkpoints": expected["checkpoints"],
            "scope": "fixture-only" if inventory["example_only"] else "configuration-only",
            "operational_hooks": "not_accepted", "ready_for_aws": False}


def observation_archive(events):
    """Normalize sanitized B envelopes without reusing business IDs as ES IDs.

    Output is a distinct archive schema, never C's synthetic event fixture.
    The caller must authenticate the archive's version/hash before recovery.
    """
    require(isinstance(events, list) and len(events) <= 100000, "bounded observation batch required")
    seen = {}
    for item in events:
        require(isinstance(item, dict) and isinstance(item.get("event"), dict)
                and isinstance(item.get("pipeline"), dict), "nested observation required")
        event, pipeline = item["event"], item["pipeline"]
        try:
            require(str(uuid.UUID(event["id"])) == event["id"], "invalid emission UUID")
            require(str(uuid.UUID(pipeline["run_id"])) == pipeline["run_id"], "invalid run UUID")
            if "business_event_id" in pipeline:
                require(str(uuid.UUID(pipeline["business_event_id"])) == pipeline["business_event_id"], "invalid business UUID")
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("invalid observation identity") from exc
        require(pipeline.get("state") in {"SENT", "ACKNOWLEDGED", "COMMITTED", "SEARCHABLE", "PUBLISHED"},
                "not a recoverable pipeline stage")
        release = pipeline.get("release_id")
        require(release is None or isinstance(release, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", release), "invalid release identity")
        normalized = {"emission_id": event["id"], "business_event_id": pipeline.get("business_event_id"),
                      "stage": pipeline["state"], "run_id": pipeline["run_id"],
                      "release_id": pipeline.get("release_id"), "source_sha256": digest(item)}
        require(event["id"] not in seen or seen[event["id"]] == normalized,
                "emission ID reused for a different stage or payload")
        seen[event["id"]] = normalized
    return {"version": "track2-observation-archive/1", "events": sorted(seen.values(), key=lambda e:e["emission_id"]),
            "authority": "observation-only", "ready_for_aws": False}
