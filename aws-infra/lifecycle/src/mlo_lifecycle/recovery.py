"""Isolated, fenced synthetic restoration. Never restarts writers or the source DB.

PG reconciliation is independent of log transport. Queue contents do not fill
gaps in either source ledger. Deletions are reapplied to every selected release.
"""

from pathlib import Path
import time

from .engine import authorize, valid_window, verify
from .journal import Journal, atomic, authenticate, locked
from .model import digest, keys, local_path, need, read
from .planner import validate_plan
from .track2 import QUERIES, events_by_id, payloads, sealed

STEPS = ("ledgers", "business-replay", "observation-replay", "checkpoints", "alias")


def outputs(spec, *, rollback=False):
    data = payloads(spec)
    source = events_by_id(data["pg-ledger"]["events"])
    archived = events_by_id(data["business-archive"])
    # Reconcile from the authoritative PG snapshot; never discard an unpublished event.
    events = {**archived, **source}
    by_entity = {}
    revisions = {}
    for event in sorted(events.values(), key=lambda e: (e["revision"], e["event_id"])):
        identity = (event["entity_id"], event["revision"])
        semantic = {k: v for k, v in event.items() if k != "event_id"}
        need(identity not in revisions or revisions[identity] == semantic, "conflicting entity revision")
        revisions[identity] = semantic
        by_entity[event["entity_id"]] = event
    deletions = dict(data["deletions"])
    for entity, event in by_entity.items():
        if event["deleted"]:
            deletions[entity] = max(event["revision"], deletions.get(entity, 0))
    business = {entity: e for entity, e in by_entity.items()
                if not e["deleted"] and e["revision"] > deletions.get(entity, -1)}
    logs = {(e["event_id"], e["stage"]): e for e in data["observation-archive"]}
    selected = data["releases"]["rollback" if rollback else "active"]
    documents = [doc for doc in data["catalog"][selected]["documents"]
                 if doc["revision"] > deletions.get(doc["entity_id"], -1)]
    return {
        "ledgers": {"postgresql": data["pg-ledger"], "mongodb": data["mongo-ledger"],
                    "preserved_assets": spec["assets"], "source_unchanged": True},
        "business-replay": {"events": sorted(business.values(), key=lambda e: e["entity_id"]),
                            "reconciled_event_ids": sorted(set(source) - set(archived)),
                            "deletion_revisions": deletions},
        "observation-replay": {"events": [logs[k] for k in sorted(logs)],
                               "stage_counts": {stage: sum(e["stage"] == stage for e in logs.values())
                                                for stage in sorted({e["stage"] for e in logs.values()})},
                               "authority": "archive-projection-only"},
        "checkpoints": {"generation": spec["recovery_generation"], "epoch": spec["epoch"],
                        "writers_started": False,
                        "queries": {query: {"path": f"aws/dev/{spec['recovery_generation']}/{query}/checkpoint/",
                                            "source_version": spec["assets"]["checkpoint-" + query]["version_id"],
                                            "source_sha256": spec["assets"]["checkpoint-" + query]["sha256"]}
                                    for query in QUERIES},
                        "activation": "requires-runtime-checkpoint-migration-and-fencing-acceptance"},
        "alias": {"release_id": selected, "documents": documents, "deletions_reapplied": True,
                  "publication": "isolated-synthetic-target", "writers_started": False},
    }


def recovery_hash(plan, spec, rollback):
    return digest({"kind": "lifecycle-track2-recovery-v1", "plan_sha256": plan["sha256"],
                   "registry_sha256": digest(spec), "rollback": rollback})


def observe(lab, spec, step, *, rollback=False):
    need(step in STEPS, "unknown recovery step")
    path = local_path(Path(lab) / "track2-recovery" / (step + ".json"))
    if not path.exists():
        return {"status": "not_started", "ready_for_aws": False}
    need(read(path) == outputs(spec, rollback=rollback)[step], "recovery output drift")
    return {"status": "completed", "sha256": digest(read(path)), "ready_for_aws": False}


def verify_recovery(plan, bundle, lab, key, *, rollback=False):
    need("track2" in bundle, "Track2 snapshot required")
    lab = local_path(lab)
    spec = bundle["track2"]
    source = verify(plan, bundle, lab, key)
    sealed(lab, spec)
    journal = Journal(lab / "track2-recovery", recovery_hash(plan, spec, rollback), key)
    latest = journal.latest()
    need(set(latest) <= set(STEPS), "unknown recovery journal step")
    completed, unknown, failed = [], [], []
    for step in STEPS:
        if step not in latest:
            continue
        try:
            current = observe(lab, spec, step, rollback=rollback)
            record = latest[step]
            if record["phase"] in {"complete", "reconciled"} and current == record["observation"]:
                completed.append(step)
            else:
                unknown.append(step)
        except (ValueError, OSError):
            failed.append(step)
    return {"schema_version": "lifecycle-track2-recovery-verification-v1", "ready_for_aws": False,
            "complete": source["complete"] and len(completed) == len(STEPS),
            "completed_steps": completed, "unknown": unknown, "failed": failed,
            "pending_steps": [s for s in STEPS if s not in latest], "writers_started": False}


def recover(plan, bundle, lab, approval, evidence, key, recovery_approval, *,
            rollback=False, resume=False, reconciliations=(), fault=None, input_guard=None):
    need("track2" in bundle and plan["mode"] == "retain-data", "Track2 retained snapshot required")
    lab = local_path(lab)
    spec = bundle["track2"]
    identity = recovery_hash(plan, spec, rollback)

    def boundary():
        now = time.time()
        validate_plan(plan, bundle, now=now)
        authorize(plan, bundle, approval, evidence, key, now)
        granted = authenticate(recovery_approval, key)
        keys(granted, "kind reviewer recovery_sha256 issued_at expires_at")
        need(granted["kind"] == "local-track2-recovery-approval-v1"
             and granted["reviewer"] == "synthetic-test-authority"
             and granted["recovery_sha256"] == identity, "recovery approval mismatch")
        valid_window(granted, now, 300)
        if input_guard:
            input_guard()
        sealed(lab, spec)
        need(verify(plan, bundle, lab, key)["complete"] is True, "shutdown snapshot not confirmed")

    with locked(lab):
        boundary()
        folder = local_path(lab / "track2-recovery")
        folder.mkdir(exist_ok=True, mode=0o700)
        journal = Journal(folder, identity, key, {"recovery_approval_sha256": digest(recovery_approval)})
        need(resume or not journal.events, "existing recovery requires resume")
        need(not resume or journal.events, "nothing to resume")
        supplied = {}
        for signed in reconciliations:
            rec = authenticate(signed, key)
            keys(rec, "kind reviewer plan_sha256 step decision observation_sha256 issued_at expires_at")
            valid_window(rec, time.time(), 300)
            need(rec["kind"] == "local-reconciliation-v1" and rec["reviewer"]
                 and rec["plan_sha256"] == identity and rec["step"] in STEPS
                 and rec["step"] not in supplied, "invalid recovery reconciliation")
            supplied[rec["step"]] = rec
        need(set(journal.latest()) <= set(STEPS), "unknown recovery journal step")
        expected = outputs(spec, rollback=rollback)
        done = []
        for step in STEPS:
            boundary()
            for prior in done:
                need(observe(lab, spec, prior, rollback=rollback) == journal.latest()[prior]["observation"],
                     "previous recovery output drift")
            current = observe(lab, spec, step, rollback=rollback)
            latest = journal.latest().get(step)
            if latest:
                if latest["phase"] in {"complete", "reconciled"}:
                    need(current["status"] == "completed" and current == latest["observation"],
                         "completed recovery changed")
                    done.append(step)
                    continue
                rec = supplied.get(step)
                need(rec is not None and rec["observation_sha256"] == digest(current),
                     "ambiguous recovery requires signed observation")
                valid_window(rec, time.time(), 300)
                if rec["decision"] == "completed":
                    need(current["status"] == "completed", "unconfirmed recovery completion")
                    journal.append(step, "reconciled", current)
                    done.append(step)
                    continue
                need(rec["decision"] == "retry" and current["status"] == "not_started",
                     "recovery retry with existing effect")
            need(current["status"] == "not_started", "untracked recovery output exists")
            journal.append(step, "started")
            try:
                if fault == [step, "before"]:
                    raise TimeoutError("synthetic before-effect failure")
                atomic(folder / (step + ".json"), expected[step])
                if fault == [step, "crash"]:
                    import os
                    os._exit(77)
                if fault == [step, "timeout"]:
                    raise TimeoutError("synthetic lost response")
                boundary()
                current = observe(lab, spec, step, rollback=rollback)
                journal.append(step, "complete", current)
            except Exception:
                journal.append(step, "ambiguous")
                raise
            done.append(step)
        return {"schema_version": "lifecycle-track2-recovery-result-v1", "ready_for_aws": False,
                "complete": True, "recovery_sha256": identity, "completed_steps": done,
                "source_generation": spec["source_generation"], "generation": spec["recovery_generation"],
                "epoch": spec["epoch"], "writers_started": False, "source_unchanged": True,
                "release_id": expected["alias"]["release_id"], "source": "synthetic-immutable-assets",
                "pitr_verified": False, "cross_engine_single_point_in_time": False}
