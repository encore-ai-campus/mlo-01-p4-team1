"""Only synthetic worker/data handoffs; never observations of the real apps."""

from pathlib import Path
import os
import time

from mlo_lifecycle.journal import atomic, sign
from mlo_lifecycle.model import digest
from mlo_lifecycle.recovery import recovery_hash
from mlo_lifecycle.track2 import QUERIES, WRITERS
from support import authorization, cli, fixture


def track2_fixture():
    bundle = fixture()
    generation = bundle["binding"]["generation"]
    bundle["schema_version"] = "lifecycle-track2-input-v2"
    events = [
        {"event_id": "event-a", "entity_id": "venue-a", "revision": 1, "deleted": False},
        {"event_id": "event-b", "entity_id": "venue-b", "revision": 2, "deleted": True},
        {"event_id": "event-c", "entity_id": "venue-c", "revision": 1, "deleted": False},
    ]
    catalog = {
        "release-one": {"documents": [{"entity_id": "venue-a", "revision": 1},
                                       {"entity_id": "venue-b", "revision": 1}]},
        "release-two": {"documents": [{"entity_id": "venue-a", "revision": 2},
                                       {"entity_id": "venue-b", "revision": 1},
                                       {"entity_id": "venue-c", "revision": 1}]},
    }
    logs = [{"event_id": "log-a", "stage": "SENT", "run_id": "run-one", "release_id": "release-two"},
            {"event_id": "log-a", "stage": "ACKNOWLEDGED", "run_id": "run-one", "release_id": "release-two"},
            {"event_id": "log-b", "stage": "COMMITTED", "run_id": "run-one", "release_id": "release-two"}]
    logs.append(dict(logs[0]))
    data = {
        "pg-ledger": {"events": events, "deletion_revisions": {"venue-b": 2}},
        "mongo-ledger": {"log_record": [{"id": "ad-one", "value": "synthetic-preserved"}]},
        "raw-manifest": {"generation": generation, "dataset_version": "fixture-v1", "path": "raw/venues/fixture-v1/", "catalog_sha256": digest(catalog)},
        "lake-manifest": {"generation": generation, "dataset_version": "fixture-v1", "path": "lake/venues/fixture-v1/v1/", "catalog_sha256": digest(catalog)},
        "catalog": catalog,
        "releases": {"active": "release-two", "rollback": "release-one",
                     "versions": {version: {"manifest_sha256": digest(value),
                                            "indexed_ids": [d["entity_id"] for d in value["documents"]]}
                                  for version, value in catalog.items()}},
        "business-archive": [events[0], events[0], events[1]],
        "observation-archive": logs, "deletions": {"venue-b": 2},
        "exporter-cursor": {"generation": generation, "event_ids": ["event-a"]},
        "publisher-cursor": {"generation": generation, "event_ids": ["event-a", "event-b"]},
        "collector-cursor": {"generation": generation, "event_ids": ["log-a", "log-b"],
                             "archive_sha256": digest(logs)},
    }
    for name in ("filebeat-queue", "logstash-queue"):
        data[name] = {"event_ids": ["log-a"], "bytes": 128, "max_bytes": 4096, "archive_sha256": digest(logs)}
    for query in QUERIES:
        data["checkpoint-" + query] = {"generation": generation, "query": query, "batch_id": 8,
                                      "path": f"aws/dev/{generation}/{query}/checkpoint/"}
    bundle["track2"] = {
        "schema_version": "lifecycle-track2-v2", "source_generation": generation, "dataset_version": "fixture-v1",
        "recovery_generation": "track2-recovery-one", "previous_epoch": 7, "epoch": 8,
        "app_workers": ["app-synthetic-worker"],
        "writers": {name: {"artifact_sha256": digest(name), "hook": "synthetic-stop-v1"}
                    for name in (*WRITERS, "app-synthetic-worker")},
        "assets": {name: {"version_id": "synthetic-version-one", "sha256": digest(value), "payload": value}
                   for name, value in data.items()},
        "retention": {"status": "unconfirmed", "automatic_delete": False},
    }
    return bundle


def prepare_track2(folder, *, rollback=False):
    folder = Path(folder)
    bundle = track2_fixture()
    atomic(folder / "bundle.json", bundle)
    plan = cli("plan", "--bundle", folder / "bundle.json", "--output", folder / "plan.json")
    cli("lab-init", "--bundle", folder / "bundle.json", "--lab", folder / "lab")
    key = os.urandom(32)
    (folder / "authority.key").write_bytes(key)
    (folder / "authority.key").chmod(0o600)
    approval, evidence = authorization(plan, bundle, key)
    atomic(folder / "approval.json", approval)
    atomic(folder / "evidence.json", evidence)
    now = time.time()
    atomic(folder / "recovery-approval.json", sign({"kind": "local-track2-recovery-approval-v1",
           "reviewer": "synthetic-test-authority",
           "recovery_sha256": recovery_hash(plan, bundle["track2"], rollback),
           "issued_at": now, "expires_at": now + 300}, key))
    args = ["--bundle", folder / "bundle.json", "--plan", folder / "plan.json", "--lab", folder / "lab",
            "--approval", folder / "approval.json", "--evidence", folder / "evidence.json",
            "--authority-key", folder / "authority.key", "--recovery-approval", folder / "recovery-approval.json"]
    if rollback:
        args.append("--rollback-release")
    return bundle, plan, key, args
