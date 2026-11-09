"""Compare source, staged transport, delivered events and a materialized snapshot.

This is an observation of supplied inputs, never proof of source retention or
a settled reporting period. No credentials, guest IDs or event payloads are returned.
"""

from .errors import AnalyticsError, CapacityExceeded, Conflict
from .events import Event, digest, timestamp
from .reporting import report


def _events(values, cutoff, max_events):
    result = {}
    duplicates = 0
    for index, event in enumerate(values):
        if index >= max_events:
            raise CapacityExceeded("AUDIT_EVENT_LIMIT")
        if not isinstance(event, Event):
            raise AnalyticsError("EXPECTED_VALIDATED_EVENT")
        if event.received_at > cutoff:
            continue
        old = result.get(event.event_id)
        if old is not None:
            if old.fingerprint != event.fingerprint:
                raise Conflict("EVENT_ID_CONFLICT")
            duplicates += 1
        result[event.event_id] = event
    return result, duplicates


def _difference(ids):
    ids = sorted(ids)
    return {"count": len(ids), "sample_event_ids": ids[:50], "sample_limit": 50}


def reconcile(
    source_events,
    staged_events,
    delivered_events,
    snapshot,
    *,
    cutoff,
    max_events=100_000,
    transport_events=None,
):
    cutoff = timestamp(cutoff)
    source, source_duplicates = _events(source_events, cutoff, max_events)
    staged, staged_duplicates = _events(staged_events, cutoff, max_events)
    delivered, delivered_duplicates = _events(delivered_events, cutoff, max_events)
    transport = None
    transport_duplicates = None
    if transport_events is not None:
        transport, transport_duplicates = _events(transport_events, cutoff, max_events)
    missing_transport = set(source) - set(transport) if transport is not None else set()
    unexpected_transport = set(transport) - set(source) if transport is not None else set()
    missing_staged = set(source) - set(staged)
    missing_delivered = set(source) - set(delivered)
    unexpected_staged = set(staged) - set(source)
    unexpected_delivered = set(delivered) - set(source)
    conflicts = {
        identity
        for identity in source
        if any(
            identity in stage and stage[identity].fingerprint != source[identity].fingerprint
            for stage in (staged, delivered, transport or {})
        )
    }
    snapshot_available = snapshot is not None
    observation_matches = False
    rows_match = False
    totals_match = False
    dataset_matches = False
    policy_matches = False
    snapshot_hash_matches = False
    if snapshot_available:
        snapshot_hash_matches = digest(
            {k: v for k, v in snapshot.items() if k != "report_id"}
        ) == snapshot.get("report_id")
        observation_matches = timestamp(snapshot["as_of"]) == cutoff
        expected = report(
            source.values(),
            cutoff=cutoff,
            settled_through=snapshot.get("settled_through"),
            max_events=max_events,
            **snapshot.get("scope", {}),
        )
        dataset_matches = expected["dataset_sha256"] == snapshot.get("dataset_sha256")
        policy_matches = expected["report_policy_version"] == snapshot.get("report_policy_version")
        rows_match = expected["rows"] == snapshot.get("rows")
        totals_match = expected["totals"] == snapshot.get("totals")
    consistent = (
        snapshot_available
        and observation_matches
        and rows_match
        and totals_match
        and dataset_matches
        and policy_matches
        and snapshot_hash_matches
        and not any(
            (
                missing_staged,
                missing_delivered,
                unexpected_staged,
                unexpected_delivered,
                missing_transport,
                unexpected_transport,
                conflicts,
            )
        )
    )
    return {
        "status": "CONSISTENT_OBSERVATION" if consistent else "MISMATCH",
        "source_completeness_verified": False,
        "as_of": cutoff.isoformat(),
        "transport_checked": transport is not None,
        "all_supplied_stages_consistent": bool(consistent and transport is not None),
        "transport_missing": _difference(missing_transport) if transport is not None else None,
        "transport_unexpected": _difference(unexpected_transport)
        if transport is not None
        else None,
        "counts": {
            "source_unique": len(source),
            "staged_unique": len(staged),
            "delivered_unique": len(delivered),
        },
        "duplicate_inputs": {
            "source": source_duplicates,
            "staged": staged_duplicates,
            "delivered": delivered_duplicates,
            "transport": transport_duplicates,
        },
        "missing_staged": _difference(missing_staged),
        "missing_delivered": _difference(missing_delivered),
        "unexpected_staged": _difference(unexpected_staged),
        "unexpected_delivered": _difference(unexpected_delivered),
        "fingerprint_conflicts": _difference(conflicts),
        "snapshot": {
            "available": snapshot_available,
            "observation_matches": observation_matches,
            "rows_match": rows_match,
            "totals_match": totals_match,
            "dataset_matches": dataset_matches,
            "policy_matches": policy_matches,
            "snapshot_hash_matches": snapshot_hash_matches,
        },
    }
