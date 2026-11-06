"""Approval/evidence validation, conservative resume, and observed verification."""

import os
import time

from .adapter import Simulator
from .journal import Journal, authenticate, locked
from .model import (
    Refused,
    binding_scope,
    code_hash,
    digest,
    keys,
    need,
    runtime_facts,
    timestamp,
)
from .planner import validate_plan


def valid_window(record, now, maximum):
    need(
        timestamp(record["issued_at"])
        and timestamp(record["expires_at"])
        and record["issued_at"] <= now < record["expires_at"]
        and 0 < record["expires_at"] - record["issued_at"] <= maximum,
        "stale/future/expired evidence or approval",
        code="authorization_failure",
    )


def _authorize(plan, bundle, approval, evidence, key, now):
    granted = authenticate(approval, key)
    keys(
        granted,
        "kind reviewer plan_sha256 scope mode delete protection_release purge_data issued_at expires_at",
    )
    need(
        granted["kind"] == "local-approval-v1"
        and granted["reviewer"] == "synthetic-test-authority",
        "reserved synthetic reviewer required",
    )
    need(
        granted["plan_sha256"] == plan["sha256"]
        and granted["scope"] == binding_scope(bundle)
        and granted["mode"] == plan["mode"]
        and granted["delete"] == plan["delete"]
        and granted["protection_release"] == plan["protection_release"],
        "approval scope/plan/protection mismatch",
    )
    purge = sorted(k for k in plan["delete"] if plan["targets"][k]["kind"] == "data")
    need(granted["purge_data"] == purge, "separate exact purge-data approval required")
    valid_window(granted, now, 3600)
    proof = authenticate(evidence, key)
    keys(
        proof, "kind plan_sha256 scope input_sha256 facts runtime issued_at expires_at"
    )
    need(
        proof["kind"] == "local-evidence-v1"
        and proof["plan_sha256"] == plan["sha256"]
        and proof["scope"] == binding_scope(bundle)
        and proof["input_sha256"] == digest(bundle),
        "missing/mixed state/identity evidence",
    )
    need(
        proof["facts"]
        == {
            "identity_verified": True,
            "state_snapshot_verified": True,
            "no_external_resources": True,
        }
        and all(v is True for v in proof["facts"].values()),
        "incomplete execution evidence",
    )
    need(
        digest(proof["runtime"]) == digest(runtime_facts(bundle)),
        "incomplete/mixed runtime evidence",
    )
    valid_window(proof, now, 300)


def authorize(plan, bundle, approval, evidence, key, now):
    try:
        _authorize(plan, bundle, approval, evidence, key, now)
    except (ValueError, KeyError, TypeError) as exc:
        raise Refused(
            "authorization/evidence rejected", code="authorization_failure"
        ) from exc


def execute(
    plan,
    bundle,
    lab,
    approval,
    evidence,
    key,
    *,
    resume=False,
    reconciliations=(),
    fault=None,
    input_guard=None,
    operation_guard=None
):
    if operation_guard is not None:
        import sys
        from .model import ROOT

        sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
        from infra_common.coordination import require_operation

        token = require_operation(operation_guard, "destroy")
        need(
            token["scope"]["generation"] == bundle["binding"]["generation"]
            and token["scope"]["account_ids"] == bundle["binding"]["account_ids"],
            "common operation scope mismatch",
        )
    now = time.time()
    validate_plan(plan, bundle, now=now)
    authorize(plan, bundle, approval, evidence, key, now)
    with locked(lab):
        adapter = Simulator(lab, bundle)
        adapter.preflight(plan["sha256"])
        journal = Journal(
            lab,
            plan["sha256"],
            key,
            {"approval_sha256": digest(approval), "evidence_sha256": digest(evidence)},
        )
        need(resume or not journal.events, "existing execution requires resume")
        if resume:
            need(bool(journal.events), "nothing to resume")
        adapter.preflight(plan["sha256"], bind=True)
        supplied = {}
        for signed in reconciliations:
            item = authenticate(signed, key)
            keys(
                item,
                "kind reviewer plan_sha256 step decision observation_sha256 issued_at expires_at",
            )
            valid_window(item, now, 300)
            need(
                item["kind"] == "local-reconciliation-v1"
                and item["reviewer"]
                and item["plan_sha256"] == plan["sha256"]
                and item["step"] not in supplied,
                "invalid/duplicate reconciliation",
            )
            supplied[item["step"]] = item
        steps = {s["id"]: s for s in plan["steps"]}
        need(
            set(journal.latest()) <= set(steps) and set(supplied) <= set(steps),
            "unknown journal/reconciliation step",
        )
        done = set()
        for step in plan["steps"]:
            if operation_guard is not None:
                require_operation(operation_guard, "destroy")
            current = time.time()
            need(
                plan["created_at"] <= current < plan["expires_at"],
                "plan expired before next step",
                code="plan_expired",
                phase="boundary",
                step_id=step["id"],
            )
            need(
                code_hash() == plan["code_sha256"]
                and digest(bundle) == plan["input_sha256"],
                "code/input changed during execution",
                code="artifact_drift",
                phase="boundary",
                step_id=step["id"],
            )
            if input_guard:
                input_guard()
            authorize(plan, bundle, approval, evidence, key, current)
            adapter.preflight(plan["sha256"])
            need(set(step["requires"]) <= done, "predecessor not complete")
            for predecessor in step["requires"]:
                observed = adapter.observe(steps[predecessor])
                need(
                    observed == journal.latest()[predecessor]["observation"],
                    "predecessor proof drifted",
                    code="state_drift",
                    phase="boundary",
                    step_id=step["id"],
                )
            latest = journal.latest().get(step["id"])
            if latest:
                observation = adapter.observe(step)
                if latest["phase"] in {"complete", "reconciled"}:
                    need(
                        observation["status"] == "completed"
                        and observation == latest["observation"],
                        "confirmed step drifted; reconciliation required",
                    )
                    done.add(step["id"])
                    continue
                decision = supplied.get(step["id"])
                need(
                    decision is not None
                    and decision["observation_sha256"] == digest(observation),
                    "ambiguous stage requires signed current observation reconciliation",
                    code="partial_effect_unknown",
                    phase="reconciliation",
                    step_id=step["id"],
                )
                valid_window(decision, time.time(), 300)
                if decision["decision"] == "completed":
                    need(
                        observation["status"] == "completed",
                        "cannot confirm unknown effect",
                    )
                    journal.append(step["id"], "reconciled", observation)
                    done.add(step["id"])
                    continue
                need(
                    decision["decision"] == "retry"
                    and observation["status"] == "not_started",
                    "retry without proven absence of effect refused",
                )
            journal.append(step["id"], "started")
            try:
                if fault == [step["id"], "before"]:
                    raise TimeoutError("injected before-effect failure")
                result = adapter.apply(step)
                if fault == [step["id"], "crash"]:
                    os._exit(77)
                if fault == [step["id"], "timeout"]:
                    raise TimeoutError("injected lost reply")
                need(
                    result["status"] == "completed",
                    "adapter did not confirm completion",
                )
                need(
                    code_hash() == plan["code_sha256"]
                    and digest(bundle) == plan["input_sha256"],
                    "artifact changed during effect",
                    code="artifact_drift",
                    phase="effect",
                    step_id=step["id"],
                )
                if input_guard:
                    input_guard()
                # Re-observe; a CLI exit alone does not complete a stop/delete.
                need(adapter.observe(step) == result, "post-action observation changed")
                journal.append(step["id"], "complete", result)
            except Exception as exc:
                journal.append(step["id"], "ambiguous")
                code = (
                    exc.code
                    if isinstance(exc, Refused)
                    else (
                        "backup_failure"
                        if step["action"] in {"stateful", "backup-data"}
                        else (
                            "backend_unavailable"
                            if isinstance(exc, OSError)
                            else "partial_effect_unknown"
                        )
                    )
                )
                raise Refused(
                    "effect unconfirmed; observe before recovery",
                    code=code,
                    phase="effect",
                    step_id=step["id"],
                ) from exc
            done.add(step["id"])
        return verify(plan, bundle, lab, key, journal=journal)


def verify(plan, bundle, lab, key, *, journal=None):
    # Report drift/failure even after approval/plan expiry; verify cannot perform any action.
    validate_plan(plan, bundle, reporting=True)
    adapter = Simulator(lab, bundle)
    adapter.preflight(plan["sha256"])
    journal = journal or Journal(lab, plan["sha256"], key)
    assets = adapter.assets()
    latest = journal.latest()
    completed, failed, unknown = [], [], []
    for step in plan["steps"]:
        record = latest.get(step["id"])
        if not record:
            continue
        try:
            observed = adapter.observe(step)
            if (
                record["phase"] in {"complete", "reconciled"}
                and observed == record["observation"]
            ):
                completed.append(step["id"])
            else:
                unknown.append(step["id"])
        except (ValueError, OSError):
            failed.append(step["id"])
    retained_missing = sorted(k for k in plan["retain"] if assets.get(k) != 1)
    need(
        set(assets) == set(plan["targets"]),
        "unexpected/missing observed resource inventory",
    )
    deleted = sorted(
        k for k in plan["delete"] if assets[k] == 0 and "delete:" + k in completed
    )
    remaining = sorted(k for k in plan["delete"] if k not in deleted)
    complete = len(completed) == len(plan["steps"]) and not (
        failed or unknown or retained_missing or remaining
    )
    target_status = {}
    for target in plan["targets"]:
        step_id = "delete:" + target
        target_status[target] = (
            "missing_retained"
            if target in retained_missing
            else (
                "retained"
                if target in plan["retain"]
                else (
                    "deleted"
                    if target in deleted
                    else (
                        "failed"
                        if step_id in failed
                        else (
                            "unknown"
                            if step_id in unknown or assets[target] == 0
                            else "planned"
                        )
                    )
                )
            )
        )
    result = {
        "schema_version": "lifecycle-result-v1",
        "ready_for_aws": False,
        "adapter": "local-simulator",
        "removal_semantics": "synthetic-logical-removal",
        "physical_resources_deleted": False,
        "physical_data_deleted": False,
        "database_engine": "SQLite",
        "backup_failure_domain": "same-local-lab",
        "plan_sha256": plan["sha256"],
        "mode": plan["mode"],
        "complete": complete,
        "planned_delete": plan["delete"],
        "deleted": deleted,
        "retained": plan["retain"],
        "remaining": remaining,
        "failed": failed,
        "unknown": unknown,
        "retained_missing": retained_missing,
        "completed_steps": completed,
        "pending_steps": [s["id"] for s in plan["steps"] if s["id"] not in latest],
        "target_status": target_status,
        "cost_may_remain": bool(plan["retain"] or remaining),
        "all_resources_deleted": False,
        "scope": "local simulator only; retained resources can continue to incur costs",
    }
    if "track2" in bundle:
        result["schema_version"] = "lifecycle-track2-result-v1"
        result["track2"] = {"registry_sha256": digest(bundle["track2"]),
                            "writers": sorted(bundle["track2"]["writers"]),
                            "retention": bundle["track2"]["retention"],
                            "hook_source": "synthetic-stop-v1", "actual_app_hooks": "not_run"}
    return result
