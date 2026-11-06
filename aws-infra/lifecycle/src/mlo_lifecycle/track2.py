"""C-owned Track2 fixture boundary. No remote hooks or operational attestations.

The explicit registry is supplied by the accepted artifact handoff. Local queue
copies are preserved evidence, never authoritative business or archive ledgers.
"""

import re

from .journal import atomic
from .model import digest, keys, need, read

WRITERS = (
    "catalog-ingest", "outbox-exporter", "mail-worker", "business-check-worker",
    "ads-writer", "weather-collector", "weather-driver", "feedback-driver",
    "analytics-driver", "catalog-driver", "search-publisher",
    "observation-writer", "observation-collector",
)
QUERIES = ("weather-stream", "feedback-postgresql", "feedback-analytics-batch", "venues-catalog")
ASSETS = {
    "pg-ledger", "mongo-ledger", "raw-manifest", "lake-manifest", "catalog",
    "releases", "business-archive", "observation-archive", "deletions",
    "exporter-cursor", "publisher-cursor", "collector-cursor",
    "filebeat-queue", "logstash-queue",
    *("checkpoint-" + query for query in QUERIES),
}
STAGES = {"SENT", "ACKNOWLEDGED", "COMMITTED", "SEARCHABLE", "PUBLISHED"}


def accept_foundation(spec, sidecar, inventory):
    from .model import ROOT
    import sys
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from infra_common.track2 import foundation_binding
    result = foundation_binding(sidecar, inventory)
    validate_spec(spec, inventory["generation"])
    need(spec["dataset_version"] == result["dataset_version"]
         and set(QUERIES) == set(result["queries"]), "lifecycle dataset/query bridge mismatch")
    return result


def accept_observation_archive(events, *, expected_sha256, version_id):
    """Read-only adapter; a versioned archive is not a business/stop receipt."""
    from .model import ROOT
    import sys
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from infra_common.track2 import observation_archive
    need(isinstance(version_id, str) and 0 < len(version_id) <= 1024
         and digest(events) == expected_sha256, "archive version/checksum missing or changed")
    return {**observation_archive(events), "source_version_id": version_id,
            "source_sha256": expected_sha256, "writers_started": False}


def count(value):
    return type(value) is int and value >= 0


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,79}", value)


def validate_spec(spec, generation):
    keys(spec, "schema_version source_generation recovery_generation dataset_version epoch previous_epoch app_workers writers assets retention")
    need(spec["schema_version"] == "lifecycle-track2-v2"
         and spec["source_generation"] == generation
         and identifier(spec["recovery_generation"])
         and spec["recovery_generation"] != generation, "Track2 generation mismatch")
    need(count(spec["previous_epoch"]) and type(spec["epoch"]) is int
         and spec["epoch"] > spec["previous_epoch"], "new fencing epoch required")
    need(isinstance(spec["dataset_version"], str)
         and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", spec["dataset_version"]),
         "invalid dataset version")
    workers = spec["app_workers"]
    need(isinstance(workers, list) and all(identifier(w) for w in workers)
         and len(workers) == len(set(workers)) and not set(workers) & set(WRITERS),
         "invalid app artifact worker registry")
    need(set(spec["writers"]) == set(WRITERS) | set(workers), "writer missing or unregistered")
    for name, writer in spec["writers"].items():
        keys(writer, "artifact_sha256 hook")
        need(isinstance(writer["artifact_sha256"], str)
             and re.fullmatch(r"[a-f0-9]{64}", writer["artifact_sha256"])
             and writer["hook"] == "synthetic-stop-v1", "only explicit synthetic hooks accepted")
    need(spec["retention"] == {"status": "unconfirmed", "automatic_delete": False}
         and spec["retention"]["automatic_delete"] is False,
         "unapproved retention/deletion policy")
    need(set(spec["assets"]) == ASSETS, "incomplete Track2 preservation inventory")
    for name, asset in spec["assets"].items():
        keys(asset, "version_id sha256 payload")
        need(isinstance(asset["version_id"], str) and identifier(asset["version_id"])
             and digest(asset["payload"]) == asset["sha256"], "unversioned or changed Track2 asset")
    validate_payloads(spec)
    return spec


def payloads(spec):
    return {name: asset["payload"] for name, asset in spec["assets"].items()}


def events_by_id(events):
    need(isinstance(events, list), "event list required")
    result = {}
    revisions = {}
    for event in events:
        keys(event, "event_id entity_id revision deleted")
        need(identifier(event["event_id"]) and identifier(event["entity_id"])
             and count(event["revision"]) and type(event["deleted"]) is bool,
             "invalid synthetic business event")
        prior = result.get(event["event_id"])
        need(prior is None or prior == event, "conflicting duplicate event_id")
        identity = (event["entity_id"], event["revision"])
        semantic = {k: v for k, v in event.items() if k != "event_id"}
        need(identity not in revisions or revisions[identity] == semantic, "conflicting entity revision")
        revisions[identity] = semantic
        result[event["event_id"]] = event
    return result


def validate_payloads(spec):
    data = payloads(spec)
    keys(data["pg-ledger"], "events deletion_revisions")
    ledger = events_by_id(data["pg-ledger"]["events"])
    archive = events_by_id(data["business-archive"])
    need(all(ledger.get(k) == v for k, v in archive.items()), "archive differs from PG source")
    need(data["deletions"] == data["pg-ledger"]["deletion_revisions"], "deletion ledger mismatch")
    for entity, revision in data["deletions"].items():
        need(identifier(entity) and count(revision), "invalid deletion revision")
    # Cursor is transport progress, not proof that an event became searchable.
    for name in ("exporter-cursor", "publisher-cursor"):
        keys(data[name], "generation event_ids")
        need(data[name]["generation"] == spec["source_generation"]
             and isinstance(data[name]["event_ids"], list)
             and len(data[name]["event_ids"]) == len(set(data[name]["event_ids"]))
             and set(data[name]["event_ids"]) <= set(ledger), "cursor ahead of source or mixed generation")
    for query in QUERIES:
        checkpoint = data["checkpoint-" + query]
        keys(checkpoint, "path generation query batch_id")
        need(checkpoint["generation"] == spec["source_generation"] and checkpoint["query"] == query
             and checkpoint["path"] == f"aws/dev/{spec['source_generation']}/{query}/checkpoint/"
             and count(checkpoint["batch_id"]), "mixed/shared checkpoint generation")
    # Immutable dataset version is independent of the recovery generation.
    for name, expected in (
        ("raw-manifest", f'raw/venues/{spec["dataset_version"]}/'),
        ("lake-manifest", f'lake/venues/{spec["dataset_version"]}/v1/'),
    ):
        keys(data[name], "generation dataset_version path catalog_sha256")
        need(data[name]["generation"] == spec["source_generation"]
             and data[name]["dataset_version"] == spec["dataset_version"]
             and data[name]["path"] == expected
             and data[name]["catalog_sha256"] == digest(data["catalog"]),
             "manifest/catalog/dataset path mismatch")
    releases = data["releases"]
    keys(releases, "active rollback versions")
    need(set(releases["versions"]) == set(data["catalog"])
         and releases["active"] in releases["versions"]
         and releases["rollback"] in releases["versions"], "unregistered alias/release")
    for version, manifest in data["catalog"].items():
        need(identifier(version), "invalid release id")
        keys(manifest, "documents")
        documents = manifest["documents"]
        need(isinstance(documents, list), "catalog documents missing")
        for doc in documents:
            keys(doc, "entity_id revision")
            need(identifier(doc["entity_id"]) and count(doc["revision"]), "invalid catalog document")
        expected = [doc["entity_id"] for doc in documents]
        indexed = releases["versions"][version]
        keys(indexed, "manifest_sha256 indexed_ids")
        need(len(expected) == len(set(expected)) and isinstance(indexed["indexed_ids"], list)
             and len(indexed["indexed_ids"]) == len(set(indexed["indexed_ids"]))
             and set(indexed["indexed_ids"]) == set(expected)
             and indexed["manifest_sha256"] == digest(manifest), "partial/mixed release cannot be published")
    logs = {}
    need(isinstance(data["observation-archive"], list), "observation archive missing")
    for event in data["observation-archive"]:
        keys(event, "event_id stage run_id release_id")
        need(identifier(event["event_id"]) and event["stage"] in STAGES
             and identifier(event["run_id"]) and event["release_id"] in data["catalog"],
             "invalid synthetic observation")
        identity = (event["event_id"], event["stage"])
        need(identity not in logs or logs[identity] == event, "conflicting archive observation")
        logs[identity] = event
    for name in ("filebeat-queue", "logstash-queue"):
        keys(data[name], "event_ids bytes max_bytes archive_sha256")
        queue = data[name]
        need(isinstance(queue["event_ids"], list)
             and set(queue["event_ids"]) <= {e[0] for e in logs}
             and count(queue["bytes"]) and count(queue["max_bytes"])
             and 0 < queue["max_bytes"] and queue["bytes"] <= queue["max_bytes"]
             and queue["archive_sha256"] == digest(data["observation-archive"]),
             "queue unbounded, unarchived or unverifiable")
    keys(data["collector-cursor"], "generation event_ids archive_sha256")
    cursor = data["collector-cursor"]
    need(cursor["generation"] == spec["source_generation"]
         and isinstance(cursor["event_ids"], list)
         and set(cursor["event_ids"]) <= {e[0] for e in logs}
         and cursor["archive_sha256"] == digest(data["observation-archive"]),
         "collector cursor/archive mismatch")
    keys(data["mongo-ledger"], "log_record")
    need(isinstance(data["mongo-ledger"]["log_record"], list), "Mongo ledger missing")


def writer_order(spec):
    # App-provided business workers drain before Drivers and telemetry closes last.
    return [*WRITERS[:6], *sorted(spec["app_workers"]), *WRITERS[6:]]


def initialize(lab, spec):
    atomic(lab / "track2-state.json", {
        "registry_sha256": digest(spec), "epoch": spec["epoch"], "old_writers": [],
        "admission_closed": False, "restarts_disabled": False, "pending_outbox": 0,
        "queue_observations_known": True,
        "writers": {name: {"epoch": spec["epoch"], "pending": 0, "unknown": 0,
                           "allow_stop": True, "stopped": False} for name in spec["writers"]},
    })
    for name, asset in spec["assets"].items():
        atomic(lab / "track2-assets" / (name + ".json"), asset)


def state(lab, spec):
    value = read(lab / "track2-state.json")
    keys(value, "registry_sha256 epoch old_writers admission_closed restarts_disabled pending_outbox queue_observations_known writers")
    need(value["registry_sha256"] == digest(spec) and value["epoch"] == spec["epoch"]
         and type(value["epoch"]) is int and value["old_writers"] == [], "old writer/fencing/registry drift")
    need(set(value["writers"]) == set(spec["writers"]), "writer observation missing")
    for writer in value["writers"].values():
        keys(writer, "epoch pending unknown allow_stop stopped")
        need(type(writer["epoch"]) is int and writer["epoch"] == spec["epoch"]
             and count(writer["pending"]) and count(writer["unknown"])
             and type(writer["allow_stop"]) is bool and type(writer["stopped"]) is bool,
             "mixed writer epoch/unknown work")
    need(type(value["admission_closed"]) is bool and type(value["restarts_disabled"]) is bool,
         "invalid admission observations")
    return value


def sealed(lab, spec):
    value = state(lab, spec)
    need(value["admission_closed"] is True and value["restarts_disabled"] is True,
         "admission/restarts not fenced")
    need(count(value["pending_outbox"]) and value["pending_outbox"] == 0
         and value["queue_observations_known"] is True, "pending Outbox/queue unknown")
    for writer in value["writers"].values():
        need(writer["stopped"] is True and writer["pending"] == writer["unknown"] == 0,
             "writer not drained/stopped")
    folder = lab / "track2-assets"
    need({p.name for p in folder.iterdir()} == {n + ".json" for n in ASSETS}, "asset missing/unregistered")
    for name, expected in spec["assets"].items():
        need(read(folder / (name + ".json")) == expected, "preserved asset/version drift")
    validate_payloads(spec)
    return {"registry_sha256": digest(spec), "assets_sha256": digest(spec["assets"]),
            "epoch": spec["epoch"], "generation": spec["source_generation"], "scope": "synthetic-hook-only"}


def apply(lab, spec, step):
    value = state(lab, spec)
    if step["action"] == "track2-freeze":
        value["admission_closed"] = value["restarts_disabled"] = True
        atomic(lab / "track2-state.json", value)
        return {"registry_sha256": digest(spec), "epoch": spec["epoch"]}
    if step["action"] == "track2-drain":
        need(value["admission_closed"] is True and value["restarts_disabled"] is True,
             "drain before admission fence")
        writer = value["writers"][step["target"]]
        need(writer["allow_stop"] is True and writer["pending"] == writer["unknown"] == 0,
             "synthetic stop hook failed/undrained")
        writer["stopped"] = True
        atomic(lab / "track2-state.json", value)
        return {"writer": step["target"], "artifact_sha256": spec["writers"][step["target"]]["artifact_sha256"],
                "epoch": spec["epoch"], "hook": "synthetic-stop-v1"}
    return sealed(lab, spec)


def observe(lab, spec, step, proof):
    value = state(lab, spec)
    action = step["action"]
    if proof is None:
        changed = ((action == "track2-freeze" and (value["admission_closed"] or value["restarts_disabled"]))
                   or (action == "track2-drain" and value["writers"][step["target"]]["stopped"]))
        return {"ready_for_aws": False, "status": "unknown" if changed else "not_started"}
    need(value["admission_closed"] is True and value["restarts_disabled"] is True,
         "Track2 admission fence lost")
    if action == "track2-drain":
        writer = value["writers"][step["target"]]
        need(writer["stopped"] is True and writer["pending"] == writer["unknown"] == 0,
             "writer revived/undrained")
    if action == "track2-seal":
        need(sealed(lab, spec) == proof, "preservation evidence changed")
    return {"ready_for_aws": False, "status": "completed", "proof": proof}
