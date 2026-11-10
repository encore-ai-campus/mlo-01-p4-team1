"""Bounded-memory full reconciliation with an owner-only disposable disk spool.

Input/event tables are never deleted or compacted. The spool is rebuilt per
observation, preserves canonical hashes, and is removed even on failure. Runtime
cost and temporary disk use remain O(retained events), not unlimited capacity.
"""
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from .errors import AnalyticsError, CapacityExceeded, Conflict
from .events import Event, canonical
from .reporting import (
    COUNTERS,
    assemble,
    day,
    in_period,
    report,
    scope_matches,
    validate_query,
)


def validate_stream_bounds(max_events=None, batch_size=1000, max_rows=5000, work_dir=None):
    if ((max_events is not None and (type(max_events) is not int or max_events < 1))
            or type(batch_size) is not int or not 1 <= batch_size <= 10_000
            or type(max_rows) is not int or not 1 <= max_rows <= 5000):
        raise AnalyticsError("INVALID_STREAM_REPORT_BOUNDS")
    if work_dir is not None and not Path(work_dir).is_dir():
        raise AnalyticsError("INVALID_REPORT_WORK_DIRECTORY")


def _stage(connection, events, cutoff, scope, max_events):
    connection.executescript("""
        PRAGMA cache_size=-1024;
        PRAGMA temp_store=FILE;
        PRAGMA journal_mode=OFF;
        CREATE TABLE events(
            event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
            delivery_id TEXT NOT NULL, eligible INTEGER NOT NULL, visible INTEGER NOT NULL,
            occurred_day TEXT NOT NULL, result TEXT NOT NULL, is_test INTEGER NOT NULL);
        CREATE INDEX event_delivery ON events(eligible,delivery_id,event_id);
        CREATE TABLE identities(delivery_id TEXT PRIMARY KEY, identity TEXT NOT NULL);
        CREATE TABLE kinds(delivery_id TEXT, kind TEXT, PRIMARY KEY(delivery_id,kind));
        CREATE TABLE aggregates(
            date_kst TEXT, ad_id TEXT, placement TEXT, media_id TEXT,
            impressions INTEGER, matched_clicks INTEGER, early_clicks INTEGER,
            unmatched_clicks INTEGER, pending_clicks INTEGER,
            PRIMARY KEY(date_kst,ad_id,placement,media_id));
        CREATE TABLE details(event_id TEXT PRIMARY KEY, body TEXT NOT NULL);
    """)
    with connection:
        for index, event in enumerate(events):
            if max_events is not None and index >= max_events:
                raise CapacityExceeded("REPORT_EVENT_LIMIT")
            if not isinstance(event, Event):
                raise AnalyticsError("EXPECTED_VALIDATED_EVENT")
            body = canonical(event.envelope())
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            old = connection.execute("SELECT fingerprint FROM events WHERE event_id=?",
                                     (event.event_id,)).fetchone()
            if old is not None:
                if old[0] != fingerprint:
                    raise Conflict("EVENT_ID_CONFLICT")
                continue
            visible = event.received_at <= cutoff and scope_matches(event, scope)
            eligible = visible and event.counted and not event.is_test
            connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (event.event_id, fingerprint, body, event.delivery_id, int(eligible),
                 int(visible), day(event.occurred_at) if visible and not event.is_test else "",
                 event.result, int(event.is_test)))
            if eligible:
                identity = canonical((event.ad_id, event.placement, event.media_id,
                                      event.creative_version))
                old = connection.execute("SELECT identity FROM identities WHERE delivery_id=?",
                                         (event.delivery_id,)).fetchone()
                if old is not None and old[0] != identity:
                    raise Conflict("DELIVERY_IDENTITY_CONFLICT")
                connection.execute("INSERT OR IGNORE INTO identities VALUES(?,?)",
                                   (event.delivery_id, identity))
                try:
                    connection.execute("INSERT INTO kinds VALUES(?,?)",
                                       (event.delivery_id, event.event_type))
                except sqlite3.IntegrityError:
                    raise Conflict("MULTIPLE_COUNTED_EVENTS_FOR_DELIVERY_TYPE") from None


def _metadata(connection, scope, batch_size):
    sha = hashlib.sha256(b"[")
    first = True
    count = tests = 0
    results = Counter()
    cursor = connection.execute(
        "SELECT body,occurred_day,result,is_test FROM events WHERE visible=1 ORDER BY event_id")
    try:
        while rows := cursor.fetchmany(batch_size):
            for body, occurred_day, result, is_test in rows:
                if not first:
                    sha.update(b",")
                sha.update(body.encode("utf-8"))
                first = False
                count += 1
                tests += is_test
                if not is_test and in_period(occurred_day, scope):
                    results[result] += 1
    finally:
        cursor.close()
    sha.update(b"]")
    return {"dataset_sha256": sha.hexdigest(), "diagnostics": {
        "source_events_as_of": count, "test_events_excluded": tests,
        "source_results_by_occurrence_day": dict(sorted(results.items())),
    }}


def _delivery_chunks(connection, batch_size):
    # Each validated delivery has at most one impression and one click. Keep a
    # complete delivery together even when batch_size=1 (at most two events).
    cursor = connection.execute(
        "SELECT delivery_id,body FROM events WHERE eligible=1 ORDER BY delivery_id,event_id")
    chunk = []
    previous = None
    try:
        while rows := cursor.fetchmany(batch_size):
            for delivery, body in rows:
                if chunk and delivery != previous and len(chunk) >= batch_size:
                    yield chunk
                    chunk = []
                chunk.append(Event.from_envelope(json.loads(body)))
                previous = delivery
        if chunk:
            yield chunk
    finally:
        cursor.close()


def _merge(connection, partial, max_rows, include_details):
    keys = ("date_kst", "ad_id", "placement", "media_id")
    updates = ",".join(f"{name}={name}+excluded.{name}" for name in COUNTERS)
    for row in partial["rows"]:
        if any(type(row.get(name)) is not int or row[name] < 0 for name in COUNTERS):
            raise AnalyticsError("INVALID_PARTIAL_REPORT_COUNTERS")
        connection.execute(
            "INSERT INTO aggregates VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(date_kst,ad_id,placement,media_id) DO UPDATE SET " + updates,
            tuple(row[key] for key in (*keys, *COUNTERS)))
    if connection.execute("SELECT COUNT(*) FROM aggregates").fetchone()[0] > max_rows:
        raise CapacityExceeded("REPORT_GROUP_LIMIT")
    if include_details:
        for detail in partial.get("click_details", []):
            connection.execute("INSERT INTO details VALUES(?,?)",
                               (detail["event_id"], canonical(detail)))
        if connection.execute("SELECT COUNT(*) FROM details").fetchone()[0] > max_rows:
            raise CapacityExceeded("REPORT_DETAIL_LIMIT")


def report_streaming(events, *, cutoff, settled_through=None, batch_size=1000,
                     max_events=None, max_rows=5000, work_dir=None, renderer=None,
                     include_details=False, **scope):
    """Match report() without a global event list, including cross-chunk joins.

    A renderer receives at most batch_size+1 complete-delivery events and can be
    the native Spark join/groupBy function. Only counters are summed; partial CTR
    percentages are never averaged. Final output rows remain explicitly bounded.
    """
    validate_stream_bounds(max_events, batch_size, max_rows, work_dir)
    cutoff, settled, scope = validate_query(cutoff, settled_through, **scope)
    renderer = renderer or report
    if not callable(renderer):
        raise AnalyticsError("INVALID_REPORT_RENDERER")
    with tempfile.TemporaryDirectory(prefix="ads-v2-report-", dir=work_dir) as folder:
        connection = sqlite3.connect(str(Path(folder) / "spool.sqlite3"))
        try:
            _stage(connection, events, cutoff, scope, max_events)
            metadata = _metadata(connection, scope, batch_size)
            for chunk in _delivery_chunks(connection, batch_size):
                options = {"include_details": True} if include_details else {}
                partial = renderer(chunk, cutoff=cutoff, settled_through=settled,
                                   max_events=len(chunk), **options, **scope)
                with connection:
                    _merge(connection, partial, max_rows, include_details)
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM aggregates ORDER BY date_kst,ad_id,placement,media_id")]
            details = ([json.loads(row[0]) for row in connection.execute(
                "SELECT body FROM details ORDER BY event_id")] if include_details else None)
            return assemble([], rows, cutoff, settled, scope, details, metadata=metadata)
        finally:
            connection.close()
