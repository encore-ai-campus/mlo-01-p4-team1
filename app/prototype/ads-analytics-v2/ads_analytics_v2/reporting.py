from collections import Counter
from datetime import date, timedelta, timezone

from .errors import AnalyticsError, CapacityExceeded, Conflict
from .events import Event, digest, stamp, timestamp

REPORT_POLICY = "ads-performance-PROPOSAL-20261106.1"
KST = timezone(timedelta(hours=9))
COUNTERS = ("impressions", "matched_clicks", "early_clicks", "unmatched_clicks", "pending_clicks")


def day(value):
    return value.astimezone(KST).date().isoformat()


def scope_matches(event, scope):
    return all(not scope.get(key) or getattr(event, key) == scope[key]
               for key in ("ad_id", "placement", "media_id"))


def validate_query(cutoff, settled_through=None, **scope):
    cutoff = timestamp(cutoff)
    settled = timestamp(settled_through) if settled_through else None
    if settled and settled > cutoff:
        raise AnalyticsError("SETTLEMENT_AFTER_CUTOFF")
    if set(scope) - {"date_from", "date_to", "ad_id", "placement", "media_id"}:
        raise AnalyticsError("UNKNOWN_REPORT_FILTER")
    try:
        start = date.fromisoformat(scope["date_from"]) if scope.get("date_from") else None
        end = date.fromisoformat(scope["date_to"]) if scope.get("date_to") else None
    except (ValueError, TypeError):
        raise AnalyticsError("INVALID_REPORT_DATE") from None
    if bool(start) != bool(end) or (start and (start > end or (end - start).days > 366)):
        raise AnalyticsError("INVALID_REPORT_RANGE")
    if scope.get("placement") not in (None, "", "top", "bottom"):
        raise AnalyticsError("INVALID_PLACEMENT")
    for key in ("ad_id", "media_id"):
        value = scope.get(key)
        if value is not None and (not isinstance(value, str) or not value or len(value) > 200):
            raise AnalyticsError("INVALID_REPORT_FILTER")
    return cutoff, settled, {key: value for key, value in scope.items() if value}


def in_period(metric_day, scope):
    return not scope.get("date_from") or scope["date_from"] <= metric_day <= scope["date_to"]


def prepare(events, cutoff, scope, max_events=100_000):
    unique = {}
    for index, event in enumerate(events):
        if index >= max_events:
            raise CapacityExceeded("REPORT_EVENT_LIMIT")
        if not isinstance(event, Event):
            raise AnalyticsError("EXPECTED_VALIDATED_EVENT")
        if event.event_id in unique and unique[event.event_id] != event:
            raise Conflict("EVENT_ID_CONFLICT")
        unique[event.event_id] = event
    visible = sorted((event for event in unique.values()
                      if event.received_at <= cutoff and scope_matches(event, scope)),
                     key=lambda event: event.event_id)
    eligible = [event for event in visible if event.counted and not event.is_test]
    by_delivery = {}
    kinds = set()
    for event in eligible:
        identity = (event.ad_id, event.placement, event.media_id, event.creative_version)
        if event.delivery_id in by_delivery and by_delivery[event.delivery_id] != identity:
            raise Conflict("DELIVERY_IDENTITY_CONFLICT")
        by_delivery[event.delivery_id] = identity
        key = (event.delivery_id, event.event_type)
        if key in kinds:
            raise Conflict("MULTIPLE_COUNTED_EVENTS_FOR_DELIVERY_TYPE")
        kinds.add(key)
    return visible, eligible


def assemble(visible, rows, cutoff, settled, scope, details=None):
    rows = sorted(rows, key=lambda row: (row["date_kst"], row["ad_id"], row["placement"], row["media_id"]))
    totals = {name: sum(row[name] for row in rows) for name in COUNTERS}
    totals["ctr_percent"] = (round(totals["matched_clicks"] * 100 / totals["impressions"], 6)
                             if totals["impressions"] else None)
    for row in rows:
        row["ctr_percent"] = (round(row["matched_clicks"] * 100 / row["impressions"], 6)
                              if row["impressions"] else None)
    counts = Counter(event.result for event in visible
                     if not event.is_test and in_period(day(event.occurred_at), scope))
    report = {
        "report_policy_version": REPORT_POLICY,
        "as_of": stamp(cutoff),
        "settled_through": stamp(settled) if settled else None,
        # A supplied boundary is not proof of source completeness or a finalized reporting period.
        "status": "PROVISIONAL",
        "classification_boundary": "CALLER_SUPPLIED" if settled else "NONE",
        "settlement_note": "CALLER_ASSERTION_NOT_VERIFIED_BY_THIS_PROTOTYPE",
        "scope": scope,
        "dataset_sha256": digest([event.envelope() for event in visible]),
        "rows": rows,
        "totals": totals,
        "diagnostics": {
            "source_events_as_of": len(visible),
            "test_events_excluded": sum(event.is_test for event in visible),
            "source_results_by_occurrence_day": dict(sorted(counts.items())),
        },
    }
    if details is not None:
        report["click_details"] = sorted(details, key=lambda item: item["event_id"])
    report["report_id"] = digest(report)
    return report


def report(events, *, cutoff, settled_through=None, include_details=False, max_events=100_000, **scope):
    cutoff, settled, scope = validate_query(cutoff, settled_through, **scope)
    visible, eligible = prepare(events, cutoff, scope, max_events)
    impressions = {event.delivery_id: event for event in eligible
                   if event.event_type == "VIEWABLE_IMPRESSION"}
    rows, details = {}, []

    def increment(event, metric_day, counter):
        if not in_period(metric_day, scope):
            return
        key = (metric_day, event.ad_id, event.placement, event.media_id)
        if key not in rows:
            rows[key] = {"date_kst": key[0], "ad_id": key[1], "placement": key[2],
                         "media_id": key[3], **dict.fromkeys(COUNTERS, 0)}
        rows[key][counter] += 1

    for event in eligible:
        if event.event_type == "VIEWABLE_IMPRESSION":
            increment(event, day(event.occurred_at), "impressions")
            continue
        impression = impressions.get(event.delivery_id)
        metric_day = day(event.occurred_at)
        if impression and impression.occurred_at <= event.occurred_at:
            status, metric_day = "matched_clicks", day(impression.occurred_at)
        elif impression:
            status = "early_clicks"
        elif settled and event.occurred_at <= settled:
            status = "unmatched_clicks"
        else:
            status = "pending_clicks"
        increment(event, metric_day, status)
        if in_period(metric_day, scope):
            details.append({"event_id": event.event_id, "delivery_id": event.delivery_id,
                            "classification": status, "date_kst": metric_day,
                            "impression_event_id": impression.event_id if status == "matched_clicks" else None})
    return assemble(visible, list(rows.values()), cutoff, settled, scope,
                    details if include_details else None)
