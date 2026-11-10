"""Regression tests for exact cutoffs and disk-spooled full observations."""
import copy
import json
import sys
import tracemalloc
import types
import unittest
from dataclasses import replace
from datetime import datetime
from unittest.mock import patch

from ads_analytics_v2.adapters.mongo import COLLECTIONS, MongoStore
from ads_analytics_v2.errors import AnalyticsError, CapacityExceeded, Conflict
from ads_analytics_v2.events import timestamp
from ads_analytics_v2.pipeline import consume_and_materialize, run_once
from ads_analytics_v2.reporting import report, validate_query
from ads_analytics_v2.sqlite_store import SQLiteStore
from ads_analytics_v2.streaming_report import report_streaming
from ads_analytics_v2.transport import MemoryBroker
from tests.helpers import CUTOFF, SETTLED, event, test_directory
from tests.test_pipeline import Source, source_row


class ReportCursor:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def batch_size(self, size):
        return self

    def __iter__(self):
        return iter(self.rows)

    def close(self):
        self.closed = True


def bson_dates(value):
    """Model BSON's exact documented millisecond Date precision without a server."""
    if isinstance(value, datetime):
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)
    if isinstance(value, dict):
        return {key: bson_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [bson_dates(item) for item in value]
    return value


def matches(row, query):
    for path, condition in query.items():
        value = row
        exists = True
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                exists = False
                value = None
                break
            value = value[part]
        if isinstance(condition, dict) and "$exists" in condition:
            if exists != condition["$exists"]:
                return False
        elif isinstance(condition, dict) and "$lte" in condition:
            if not exists or value > condition["$lte"]:
                return False
        elif value != condition:
            return False
    return True


class ReportCollection:
    def __init__(self):
        self.rows = []
        self.cursors = []

    def insert_one(self, value):
        self.rows.append(bson_dates(copy.deepcopy(value)))

    def find_one(self, query, sort=None):
        rows = [row for row in self.rows if matches(row, bson_dates(query))]
        for name, direction in reversed(sort or []):
            rows.sort(key=lambda row: row[name], reverse=direction < 0)
        return copy.deepcopy(rows[0]) if rows else None

    def find(self, query, projection=None):
        cursor = ReportCursor([copy.deepcopy(row) for row in self.rows
                               if matches(row, bson_dates(query))])
        self.cursors.append(cursor)
        return cursor


class ReportDatabase:
    name = "bounded-test-sink"

    def __init__(self):
        self.collections = {}

    def __getitem__(self, key):
        return self.collections.setdefault(key, ReportCollection())


class MongoPrecisionTests(unittest.TestCase):
    def setUp(self):
        # Mongo is an optional dependency. Inject only the exception import when
        # it is unavailable; no client or network operation is used by this fake.
        try:
            import pymongo.errors  # noqa: F401
        except ImportError:
            module = types.ModuleType("pymongo")
            module.__path__ = []
            errors = types.ModuleType("pymongo.errors")
            errors.DuplicateKeyError = type("DuplicateKeyError", (Exception,), {})
            module.errors = errors
            self.modules = patch.dict(sys.modules, {"pymongo": module, "pymongo.errors": errors})
            self.modules.start()
            self.addCleanup(self.modules.stop)
        self.database = ReportDatabase()
        self.store = MongoStore(self.database)
        self.collection = self.database[COLLECTIONS["reports"]]

    def legacy(self, value, saved=0):
        self.collection.insert_one({"_id": value["report_id"], "body": value,
            "saved_at_ns": saved, "report_as_of": timestamp(value["as_of"])})

    def test_exact_cutoff_excludes_future_snapshot_in_same_bson_millisecond(self):
        older = report([], cutoff="2026-11-06T01:10:00.000050Z")
        future = report([], cutoff="2026-11-06T01:10:00.000900Z")
        self.store.save_report(older)
        self.store.save_report(future)
        self.assertEqual(self.collection.rows[0]["report_as_of"],
                         self.collection.rows[1]["report_as_of"])
        actual = self.store.latest_report(cutoff="2026-11-06T01:10:00.000100Z")
        self.assertEqual(actual, older)

    def test_as_of_priority_is_not_replaced_by_same_millisecond_save_order(self):
        older = report([], cutoff="2026-11-06T01:10:00.000050Z")
        newer = report([], cutoff="2026-11-06T01:10:00.000900Z")
        self.store.save_report(newer)
        self.store.save_report(older)
        self.assertEqual(self.store.latest_report(), newer)

    def test_legacy_and_new_snapshots_are_merged_without_rewriting(self):
        older = report([], cutoff="2026-11-06T01:10:00.000050Z")
        future = report([], cutoff="2026-11-06T01:10:00.000900Z")
        self.legacy(older, saved=1)
        self.store.save_report(future)
        before = copy.deepcopy(self.collection.rows)
        self.assertEqual(self.store.latest_report(
            cutoff=timestamp("2026-11-06T01:10:00.000100Z")), older)
        self.assertEqual(self.collection.rows, before)
        self.assertTrue(all(cursor.closed for cursor in self.collection.cursors))
        self.assertIsNone(self.store.latest_report(cutoff="2026-11-06T01:10:00.000001Z"))

    def test_legacy_future_is_not_selected_over_an_eligible_new_snapshot(self):
        older = report([], cutoff="2026-11-06T01:10:00.000050Z")
        future = report([], cutoff="2026-11-06T01:10:00.000900Z")
        self.store.save_report(older)
        self.legacy(future, saved=10**19)
        self.assertEqual(self.store.latest_report(
            cutoff="2026-11-06T01:10:00.000100Z"), older)
        self.assertEqual(self.store.latest_report(), future)


class BoundedReportTests(unittest.TestCase):
    def setUp(self):
        self.directory_context = test_directory()
        self.directory = self.directory_context.__enter__()
        self.addCleanup(self.directory_context.__exit__, None, None, None)
        self.sink = SQLiteStore(":memory:")
        self.producer = SQLiteStore(":memory:")
        self.addCleanup(self.sink.close)
        self.addCleanup(self.producer.close)

    def streaming(self, events, **options):
        return report_streaming(events, cutoff=CUTOFF, work_dir=self.directory, **options)

    def test_chunk_boundary_duplicates_future_and_all_hashes_match_reference(self):
        impression = event("z-impression", delivery="pair",
            occurred="2026-11-05T14:59:59Z", received="2026-11-05T15:00:01Z")
        click = event("a-click", delivery="pair", kind="CLICK",
            occurred="2026-11-05T15:00:02Z", received="2026-11-05T15:00:03Z")
        excluded = event("excluded", delivery="excluded", counted=False,
                         result="MEASUREMENT_UNAVAILABLE", is_test=True)
        future = event("future", delivery="future", occurred="2026-11-07T00:00:00Z")
        unmatched = event("unmatched", delivery="unmatched", kind="CLICK")
        values = [click, future, excluded, unmatched, impression, click]
        for size in (1, 2, 3, 7):
            with self.subTest(chunk_size=size):
                actual = self.streaming(iter(values), batch_size=size,
                    settled_through=SETTLED, include_details=True)
                expected = report(values, cutoff=CUTOFF, settled_through=SETTLED,
                                  include_details=True)
                self.assertEqual(actual, expected)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_late_impression_replay_corrects_pending_click_across_chunks(self):
        click = event("a-click", kind="CLICK", occurred="2026-11-06T00:00:02Z")
        impression = event("z-impression", occurred="2026-11-06T00:00:01Z",
                           received="2026-11-06T00:01:00Z")
        def payload(item):
            return item.delivery_id.encode(), json.dumps(item.envelope()).encode()
        consume_and_materialize(self.sink, [payload(click)], cutoff=CUTOFF,
                                batch_size=1, work_dir=self.directory)
        self.assertEqual(self.sink.latest_report()["totals"]["pending_clicks"], 1)
        value = consume_and_materialize(self.sink, [payload(impression), payload(click)],
            cutoff=CUTOFF, batch_size=1, work_dir=self.directory)
        self.assertEqual(value["inserted"], 1)
        self.assertEqual(value["report"], report([click, impression], cutoff=CUTOFF))

    def test_conflicts_still_fail_when_their_events_are_far_apart(self):
        first = event("a")
        cases = [
            [first, replace(first, ad_id="conflict")],
            [first, event("z")],
            [first, event("z", kind="CLICK", ad_id="conflict")],
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(Conflict):
                self.streaming(iter(values), batch_size=1)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_scope_dates_are_normalized_before_filtering_and_hashing(self):
        _, _, scope = validate_query(CUTOFF, date_from="20261106", date_to="2026-W45-5")
        self.assertEqual(scope, {"date_from": "2026-11-06", "date_to": "2026-11-06"})
        values = [event()]
        actual = self.streaming(values, date_from="20261106", date_to="2026-W45-5",
                                batch_size=1)
        self.assertEqual(actual, report(values, cutoff=CUTOFF,
                                      date_from="2026-11-06", date_to="2026-11-06"))
        self.assertEqual(actual["totals"]["impressions"], 1)

    def test_renderer_receives_complete_bounded_deliveries(self):
        values = [event(f"i-{i}", delivery=f"d-{i}") for i in range(9)]
        values += [event(f"c-{i}", delivery=f"d-{i}", kind="CLICK") for i in range(9)]
        sizes = []

        def renderer(chunk, **options):
            sizes.append(len(chunk))
            self.assertLessEqual(len(chunk), 4)
            deliveries = {item.delivery_id for item in chunk}
            self.assertTrue(all(sum(item.delivery_id == identity for item in chunk) == 2
                                for identity in deliveries))
            return report(chunk, **options)

        actual = self.streaming(values, batch_size=3, renderer=renderer)
        self.assertEqual(actual, report(values, cutoff=CUTOFF))
        self.assertGreater(len(sizes), 1)

    def test_explicit_caps_and_cleanup_are_preserved(self):
        with self.assertRaises(CapacityExceeded):
            self.streaming([event("a"), event("b", delivery="b")],
                           max_events=1, batch_size=1)
        with self.assertRaises(CapacityExceeded):
            self.streaming([event("a"), event("b", delivery="b", ad_id="ad-b")],
                           max_rows=1, batch_size=1)

        def fail(*args, **kwargs):
            raise RuntimeError("injected renderer failure")

        with self.assertRaises(RuntimeError):
            self.streaming([event()], renderer=fail)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_bad_run_once_configuration_has_no_source_or_sink_side_effects(self):
        for options in ({"publish_limit": 0}, {"page_size": 0}, {"max_events": 0},
                        {"batch_size": 0}, {"aggregate": False}, {"consumer": False},
                        {"work_dir": self.directory / "missing"}):
            with self.subTest(options=options), self.assertRaises(AnalyticsError):
                source = Source([source_row(event())])
                run_once(source, self.producer, MemoryBroker(), self.sink,
                    source_id="s", media_id="muther-test", cutoff=CUTOFF,
                    clock=lambda: 1, **options)
            self.assertEqual(source.queries, [])
            self.assertEqual(self.producer.read_events(), [])
            self.assertIsNone(self.producer.capture_state("s"))
            self.assertEqual(self.producer.queue_counts(), {})

    def test_bad_cutoff_is_rejected_before_sink_ingestion(self):
        message = (b"d", json.dumps(event().envelope()).encode())
        with self.assertRaises(AnalyticsError):
            consume_and_materialize(self.sink, [message], cutoff="invalid",
                                    work_dir=self.directory)
        self.assertEqual(self.sink.read_events(), [])

    def test_100001_retained_events_keep_hashes_with_bounded_memory(self):
        count = 100001
        self.sink.ingest(event(f"event-{i:06d}", delivery=f"delivery-{i:06d}")
                         for i in range(count))
        expected = report(self.sink.iter_events(batch_size=127), cutoff=CUTOFF,
                          max_events=count)
        tracemalloc.start()
        try:
            with patch.object(self.sink, "read_events", side_effect=AssertionError(
                    "Production materialization must not load the full history list")):
                actual = consume_and_materialize(self.sink, [], cutoff=CUTOFF,
                    batch_size=127, work_dir=self.directory)["report"]
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual, expected)
        self.assertEqual(actual["totals"]["impressions"], count)
        self.assertLess(peak, 16 * 1024 * 1024)
        self.assertEqual(self.sink.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                         count)
        self.assertEqual(list(self.directory.iterdir()), [])
