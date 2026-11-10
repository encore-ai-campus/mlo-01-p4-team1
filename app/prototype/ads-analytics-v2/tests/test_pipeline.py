"""Restart, reconciliation and transport boundaries; no external services."""
import json
import unittest
from unittest.mock import MagicMock
from dataclasses import replace

from ads_analytics_v2.adapters.source_snapshot import capture_v1_page
from ads_analytics_v2.errors import AnalyticsError, CapacityExceeded, Conflict
from ads_analytics_v2.events import timestamp
from ads_analytics_v2.pipeline import consume_and_materialize, run_once
from ads_analytics_v2.reporting import report
from ads_analytics_v2.sqlite_store import SQLiteStore
from ads_analytics_v2.transport import MemoryBroker, publish_once
from tests.helpers import CUTOFF, event, test_directory


def source_row(item):
    value = item.envelope()
    value["ads_policy_version"] = value.pop("source_policy_version")
    value.pop("media_id")
    value.pop("event_schema_version")
    return value


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, name, direction):
        assert name == "event_id" and direction in (-1, 1)
        self.rows.sort(key=lambda row: row["event_id"], reverse=direction == -1)
        return self

    def limit(self, count):
        return self.rows[:count]


class Source:
    """Only a read interface exists, so capture cannot silently mutate V1."""
    name = "ad_events"

    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.projections = []

    def find(self, query, projection):
        self.queries.append(query)
        self.projections.append(projection)
        selected = [row for row in self.rows
                    if timestamp(row["received_at"]) <= query["received_at"]["$lte"]
                    and row["event_id"] > query.get("event_id", {}).get("$gt", "")
                    and row["event_id"] <= query.get("event_id", {}).get("$lte", "\uffff")]
        return Cursor([dict(row) for row in selected])


class CapturePipelineTests(unittest.TestCase):
    def setUp(self):
        self.producer = SQLiteStore(":memory:")
        self.sink = SQLiteStore(":memory:")
        self.addCleanup(self.producer.close)
        self.addCleanup(self.sink.close)

    def capture(self, source, store=None, **options):
        return capture_v1_page(source, store or self.producer, source_id="reviewed-source",
                               media_id="muther-test", cutoff=CUTOFF, **options)

    def test_durable_restart_same_timestamp_and_late_commit_behind_cursor(self):
        source = Source([source_row(event("b", delivery="b")), source_row(event("c", delivery="c"))])
        with test_directory() as directory:
            path = directory / "producer.sqlite3"
            first = SQLiteStore(path)
            page = self.capture(source, first, page_size=1)
            self.assertEqual(page["state"]["after_event_id"], "b")
            first.close()
            # This transaction becomes visible late, with an old received_at and ID.
            source.rows.append(source_row(event("a", delivery="a")))
            resumed = SQLiteStore(path)
            try:
                page = self.capture(source, resumed, page_size=1)
                self.assertTrue(page["sweep_complete"])
                self.assertEqual([item.event_id for item in resumed.read_events()], ["b", "c"])
                page = self.capture(source, resumed, page_size=1)
                self.assertEqual(page["inserted"], 1)
                self.assertEqual([item.event_id for item in resumed.read_events()], ["a", "b", "c"])
                while not page["sweep_complete"]:
                    page = self.capture(source, resumed, page_size=1)
                self.assertEqual(resumed.queue_counts(), {"PENDING": 3})
            finally:
                resumed.close()

    def test_invalid_lookahead_does_not_commit_any_event_or_progress(self):
        rows = [source_row(event("a")), source_row(event("b", delivery="b"))]
        rows[1]["counted"] = False
        source = Source(rows)
        with self.assertRaises(AnalyticsError):
            self.capture(source, page_size=1)
        self.assertIsNone(self.producer.capture_state("reviewed-source"))
        self.assertEqual(self.producer.read_events(), [])
        self.assertEqual(self.producer.queue_counts(), {})
        rows[1]["counted"] = True
        self.assertEqual(self.capture(source, page_size=2)["inserted"], 2)

    def test_conflicting_increment_rolls_back_new_event_and_cursor(self):
        self.producer.ingest([replace(event("b", delivery="b"), ad_id="different")])
        source = Source([source_row(event("a")), source_row(event("b", delivery="b"))])
        with self.assertRaises(Conflict):
            self.capture(source)
        self.assertEqual([item.event_id for item in self.producer.read_events()], ["b"])
        self.assertIsNone(self.producer.capture_state("reviewed-source"))
        self.assertEqual(self.producer.queue_counts(), {})

    def test_compare_and_swap_prevents_stale_page_progress(self):
        self.producer.ingest_capture([event("a")], source_id="s", expected_version=0,
                                     state={"cursor": "a"})
        with self.assertRaises(Conflict):
            self.producer.ingest_capture([event("b")], source_id="s", expected_version=0,
                                         state={"cursor": "b"})
        self.assertEqual([item.event_id for item in self.producer.read_events()], ["a"])
        self.assertEqual(self.producer.capture_state("s"), {"cursor": "a", "version": 1})

    def test_source_binding_change_is_rejected(self):
        source = Source([source_row(event())])
        self.capture(source)
        with self.assertRaises(Conflict):
            capture_v1_page(source, self.producer, source_id="reviewed-source",
                            media_id="other-media", cutoff=CUTOFF)
        self.assertEqual(self.producer.capture_state("reviewed-source")["version"], 1)
        self.assertNotIn("delivery_token", source.projections[0])
        self.assertNotIn("owner_guest_id", source.projections[0])

    def test_sweep_cutoff_is_fixed_until_complete(self):
        later = "2026-11-06T03:00:00Z"
        source = Source([source_row(event("a")), source_row(event("b", delivery="b")),
                         source_row(event("c", delivery="c", occurred=later))])
        page = self.capture(source, page_size=1)
        self.assertFalse(page["sweep_complete"])
        page = capture_v1_page(source, self.producer, source_id="reviewed-source",
                               media_id="muther-test", cutoff=later, page_size=1)
        self.assertTrue(page["sweep_complete"])
        self.assertEqual(len(self.producer.read_events()), 2)
        page = capture_v1_page(source, self.producer, source_id="reviewed-source",
                               media_id="muther-test", cutoff=later)
        self.assertEqual(page["inserted"], 1)

    def test_no_consumer_does_not_fabricate_sink_completion(self):
        source = Source([source_row(event())])
        broker = MemoryBroker()
        value = run_once(source, self.producer, broker, self.sink,
                         source_id="s", media_id="muther-test", cutoff=CUTOFF, clock=lambda: 1)
        self.assertEqual(value["publish"]["sent"], 1)
        self.assertIsNone(value["consume"])
        self.assertEqual(self.sink.read_events(), [])
        self.assertIsNone(self.sink.latest_report())

    def test_actual_transport_replay_materializes_distinct_sink(self):
        source = Source([source_row(event()), source_row(event(
            "click", kind="CLICK", occurred="2026-11-06T00:00:01Z"))])
        broker = MemoryBroker()
        value = run_once(source, self.producer, broker, self.sink, source_id="s",
            media_id="muther-test", cutoff=CUTOFF, clock=lambda: 1,
            consumer=lambda: broker.messages + broker.messages[:1])
        self.assertEqual(value["publish"]["sent"], 2)
        self.assertEqual(value["consume"]["received"], 3)
        self.assertEqual(value["consume"]["inserted"], 2)
        snapshot = self.sink.latest_report()
        self.assertEqual(snapshot["scope"], {})
        self.assertEqual(snapshot["totals"]["impressions"], 1)
        self.assertEqual(snapshot["totals"]["matched_clicks"], 1)
        self.assertEqual(snapshot["totals"]["ctr_percent"], 100)
        self.assertIsNone(self.producer.latest_report())

    def test_same_store_and_same_sqlite_path_are_rejected_before_capture(self):
        source = Source([source_row(event())])
        with self.assertRaises(AnalyticsError):
            run_once(source, self.producer, MemoryBroker(), self.producer,
                     source_id="s", media_id="muther-test", cutoff=CUTOFF)
        self.assertEqual(source.queries, [])
        with test_directory() as directory:
            first = SQLiteStore(directory / "shared.sqlite3")
            second = SQLiteStore(directory / "shared.sqlite3")
            try:
                with self.assertRaises(AnalyticsError):
                    run_once(source, first, MemoryBroker(), second, source_id="s",
                             media_id="muther-test", cutoff=CUTOFF)
            finally:
                first.close()
                second.close()

    def test_materialization_failure_can_replay_after_process_restart(self):
        messages = [(b"d", json.dumps(event().envelope()).encode())]
        with test_directory() as directory:
            path = directory / "sink.sqlite3"
            sink = SQLiteStore(path)

            def fail(*args, **kwargs):
                raise RuntimeError("injected aggregation failure")

            with self.assertRaises(RuntimeError):
                consume_and_materialize(sink, messages, cutoff=CUTOFF, aggregate=fail)
            self.assertIsNone(sink.latest_report())
            sink.close()
            sink = SQLiteStore(path)
            try:
                value = consume_and_materialize(sink, messages, cutoff=CUTOFF)
                self.assertEqual(value["inserted"], 0)
                self.assertEqual(sink.latest_report()["totals"]["impressions"], 1)
            finally:
                sink.close()

    def test_invalid_or_oversized_transport_batch_is_not_partially_ingested(self):
        valid = (b"d", json.dumps(event().envelope()).encode())
        with self.assertRaises(AnalyticsError):
            consume_and_materialize(self.sink, [valid, (b"wrong", valid[1])], cutoff=CUTOFF)
        self.assertEqual(self.sink.read_events(), [])
        with self.assertRaises(CapacityExceeded):
            consume_and_materialize(self.sink, [valid, valid], cutoff=CUTOFF, max_messages=1)
        self.assertEqual(self.sink.read_events(), [])

    def test_latest_full_snapshot_cutoff_and_same_cutoff_late_correction(self):
        old = report([], cutoff=CUTOFF)
        newer = report([event()], cutoff=CUTOFF)
        self.sink.save_report(old)
        self.sink.save_report(newer)
        future = report([event()], cutoff="2026-11-07T00:00:00Z")
        self.sink.save_report(future)
        filtered = report([event()], cutoff="2026-11-08T00:00:00Z", ad_id="ad-one")
        self.sink.save_report(filtered)
        self.assertEqual(self.sink.latest_report()["report_id"], future["report_id"])
        self.assertEqual(self.sink.latest_report(cutoff=CUTOFF)["report_id"], newer["report_id"])
        self.assertIsNone(self.sink.latest_report(cutoff="2026-11-05T00:00:00Z"))

    def test_dead_requires_explicit_atomic_requeue_with_audit(self):
        self.producer.ingest([event()], enqueue=True)
        claim = self.producer.claim(now=0, max_attempts=1)
        self.producer.release(claim, now=1, delay=0)
        self.assertIsNone(self.producer.claim(now=2, max_attempts=1))
        self.assertEqual(self.producer.dead_letters()[0]["event_id"], "i")
        with self.assertRaises(Conflict):
            self.producer.requeue_dead(["i", "missing"], now=3)
        self.assertEqual(self.producer.queue_counts(), {"DEAD": 1})
        self.assertEqual(self.producer.requeue_dead(["i"], now=3), 1)
        broker = MemoryBroker()
        self.assertEqual(publish_once(self.producer, broker, clock=lambda: 4)["sent"], 1)
        self.assertEqual(self.producer.queue_counts(), {"SENT": 1})
        self.assertEqual(self.producer.connection.execute(
            "SELECT previous_attempts FROM outbox_requeues").fetchone()[0], 1)
        with self.assertRaises(Conflict):
            self.producer.requeue_dead(["i"], now=5)

    def test_new_arrivals_do_not_extend_the_current_sweep_indefinitely(self):
        source = Source([source_row(event("a")), source_row(event("b", delivery="b"))])
        self.assertFalse(self.capture(source, page_size=1)["sweep_complete"])
        source.rows.append(source_row(event("c", delivery="c")))
        self.assertTrue(self.capture(source, page_size=1)["sweep_complete"])
        self.assertEqual(len(self.producer.read_events()), 2)
        self.assertEqual(self.capture(source)["inserted"], 1)

    def test_v1_persisted_record_shape_is_whitelisted_and_required_fields_fail_closed(self):
        # Same persisted document shape as public_service.record_event, including
        # aware BSON datetimes and fields that must not enter the analytics stream.
        row = source_row(event("real-shape"))
        row.update(
            _id=row["event_id"], schema_version="phase3-ads-public-20261015.2",
            occurred_at=timestamp(row["occurred_at"]), received_at=timestamp(row["received_at"]),
            processed_at=timestamp(CUTOFF), accepted_at=timestamp(row["occurred_at"]),
            owner_guest_id="synthetic-private-guest", request_hash="synthetic-hash",
            request_payload={"event_id": row["event_id"], "delivery_token": "synthetic-private"},
            metric_date_kst="2026-11-06", estimated_tokens=2,
            response_snapshot={"counted": True}, retention_due_at=None, log_record={"private": True},
        )
        self.assertEqual(self.capture(Source([row]))["inserted"], 1)
        projected = self.producer.read_events()[0].envelope()
        for forbidden in ("owner_guest_id", "request_payload", "response_snapshot",
                          "estimated_tokens", "log_record", "processed_at"):
            self.assertNotIn(forbidden, projected)
        for missing in ("event_id", "ads_policy_version"):
            with self.subTest(missing=missing):
                target = SQLiteStore(":memory:")
                try:
                    invalid = dict(row)
                    invalid.pop(missing)
                    source = MagicMock()
                    source.name = "ad_events"
                    # The ceiling read is separate from the validated data page.
                    source.find.return_value.sort.return_value.limit.side_effect = [
                        [{"event_id": row["event_id"]}], [invalid]]
                    with self.assertRaises(AnalyticsError):
                        self.capture(source, target)
                    self.assertIsNone(target.capture_state("reviewed-source"))
                    self.assertEqual(target.read_events(), [])
                finally:
                    target.close()
