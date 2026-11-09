"""Read-only snapshots and durable cyclic reconciliation; no V1 writes or CDC."""

from ..errors import AnalyticsError, CapacityExceeded
from ..events import Event, timestamp

SOURCE_FIELDS = (
    "event_id",
    "delivery_id",
    "ad_id",
    "creative_version",
    "placement",
    "event_type",
    "occurred_at",
    "received_at",
    "result",
    "counted",
    "is_test",
    "ads_policy_version",
)


def capture_v1_snapshot(
    source_collection, target_store, *, media_id, cutoff, max_rows=10_000, now=0
):
    """The caller supplies authorized source/target clients, potentially on Control.

    Full bounded rescans avoid pretending a timestamp cursor is commit-order safe.
    Concurrent commits, source retention and actual completeness need reconciliation.
    No cursor/resume token, source updates or automatic network bootstrap is provided.
    """
    if source_collection.name != "ad_events" or not 1 <= max_rows <= 100_000:
        raise AnalyticsError("INVALID_SOURCE_CAPTURE")
    projection = {name: 1 for name in SOURCE_FIELDS}
    projection["_id"] = 0
    rows = list(
        source_collection.find({"received_at": {"$lte": timestamp(cutoff)}}, projection)
        .sort("_id", 1)
        .limit(max_rows + 1)
    )
    if len(rows) > max_rows:
        raise CapacityExceeded("SOURCE_CAPTURE_LIMIT")
    events = [Event.from_v1_record(row, media_id=media_id) for row in rows]
    return {
        "source_rows": len(events),
        "inserted": target_store.ingest(events, enqueue=True, now=now),
        "source_completeness_verified": False,
    }


def capture_v1_page(
    source_collection, target_store, *, source_id, media_id, cutoff, page_size=1000, now=0
):
    """Commit one read-only source page and its durable CAS progress together.

    A sweep fixes its observation cutoff and scans immutable event IDs. On reaching
    the end, the next call starts from the beginning, not a received_at high-water
    mark. A transaction committed late behind the cursor is thus seen on the next
    sweep while the source retains it. This is reconciliation, not CDC or proof
    of completeness. Source retention must exceed the sweep/retry delay.
    """
    import re

    from ..errors import Conflict
    from ..events import stamp
    from ..sqlite_store import validate_claim

    if (
        source_collection.name != "ad_events"
        or type(page_size) is not int
        or not 1 <= page_size <= 10_000
        or not isinstance(source_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", source_id)
    ):
        raise AnalyticsError("INVALID_SOURCE_CAPTURE")
    validate_claim(now, 60, 8)
    requested_cutoff = timestamp(cutoff)
    previous = target_store.capture_state(source_id)
    if previous and (
        previous.get("media_id") != media_id
        or previous.get("source_collection") != source_collection.name
    ):
        raise Conflict("CAPTURE_SOURCE_BINDING_CHANGED")
    resuming = previous is not None and previous["in_progress"]
    sweep_cutoff = timestamp(previous["cutoff"]) if resuming else requested_cutoff
    if previous and not resuming and sweep_cutoff < timestamp(previous["cutoff"]):
        raise AnalyticsError("CAPTURE_CUTOFF_REGRESSION")
    after = previous["after_event_id"] if resuming else None
    if resuming:
        upper = previous["upper_event_id"]
    else:
        # A finite ID ceiling prevents a busy source extending a sweep forever.
        ceiling = list(
            source_collection.find(
                {"received_at": {"$lte": sweep_cutoff}}, {"event_id": 1, "_id": 0}
            )
            .sort("event_id", -1)
            .limit(1)
        )
        upper = ceiling[0]["event_id"] if ceiling else None
        if upper is not None:
            from ..events import identifier

            identifier(upper)
    query = {"received_at": {"$lte": sweep_cutoff}}
    if upper is not None:
        query["event_id"] = {"$lte": upper}
        if after is not None:
            query["event_id"]["$gt"] = after
    projection = {name: 1 for name in SOURCE_FIELDS}
    projection["_id"] = 0
    rows = (
        list(source_collection.find(query, projection).sort("event_id", 1).limit(page_size + 1))
        if upper is not None
        else []
    )
    if len(rows) > page_size + 1:
        raise CapacityExceeded("SOURCE_PAGE_LIMIT")
    # Validate the look-ahead too: never advance past an invalid/duplicate ID.
    validated = [Event.from_v1_record(row, media_id=media_id) for row in rows]
    ids = [event.event_id for event in validated]
    if (
        ids != sorted(set(ids))
        or (after is not None and ids and ids[0] <= after)
        or any(event.received_at > sweep_cutoff for event in validated)
    ):
        raise Conflict("SOURCE_PAGE_ORDER_CONFLICT")
    events = validated[:page_size]
    more = len(validated) > page_size
    state = {
        "schema_version": 1,
        "source_collection": source_collection.name,
        "media_id": media_id,
        "cutoff": stamp(sweep_cutoff),
        "upper_event_id": upper,
        "after_event_id": events[-1].event_id if more else None,
        "in_progress": more,
        "completed_sweeps": (previous["completed_sweeps"] if previous else 0) + int(not more),
        "pages_in_sweep": (previous["pages_in_sweep"] if resuming else 0) + 1,
        "rows_in_sweep": (previous["rows_in_sweep"] if resuming else 0) + len(events),
    }
    version = previous["version"] if previous else 0
    inserted = target_store.ingest_capture(
        events, source_id=source_id, expected_version=version, state=state, now=now
    )
    return {
        "source_rows": len(events),
        "inserted": inserted,
        "sweep_complete": not more,
        "state": {**state, "version": version + 1},
        "source_completeness_verified": False,
    }
