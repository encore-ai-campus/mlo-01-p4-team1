"""Explicit bounded read-only capture, not a continuously installed V1 hook/CDC."""
from ..errors import AnalyticsError, CapacityExceeded
from ..events import Event, timestamp

SOURCE_FIELDS = (
    "event_id", "delivery_id", "ad_id", "creative_version", "placement", "event_type",
    "occurred_at", "received_at", "result", "counted", "is_test", "ads_policy_version",
)


def capture_v1_snapshot(source_collection, target_store, *, media_id, cutoff, max_rows=10_000, now=0):
    """The caller supplies authorized source/target clients, potentially on Control.

    Full bounded rescans avoid pretending a timestamp cursor is commit-order safe.
    Concurrent commits, source retention and actual completeness need reconciliation.
    No cursor/resume token, source updates or automatic network bootstrap is provided.
    """
    if source_collection.name != "ad_events" or not 1 <= max_rows <= 100_000:
        raise AnalyticsError("INVALID_SOURCE_CAPTURE")
    projection = {name: 1 for name in SOURCE_FIELDS}
    projection["_id"] = 0
    rows = list(source_collection.find({"received_at": {"$lte": timestamp(cutoff)}}, projection)
                .sort("_id", 1).limit(max_rows + 1))
    if len(rows) > max_rows:
        raise CapacityExceeded("SOURCE_CAPTURE_LIMIT")
    events = [Event.from_v1_record(row, media_id=media_id) for row in rows]
    return {"source_rows": len(events), "inserted": target_store.ingest(events, enqueue=True, now=now),
            "source_completeness_verified": False}
