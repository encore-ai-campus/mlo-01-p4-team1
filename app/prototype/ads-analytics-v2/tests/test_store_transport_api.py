import io
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from ads_analytics_v2.adapters.kafka import KafkaBroker
from ads_analytics_v2.api import application
from ads_analytics_v2.cli import local_path, main
from ads_analytics_v2.errors import AnalyticsError, CapacityExceeded, Conflict, LeaseLost
from ads_analytics_v2.reporting import report
from ads_analytics_v2.sqlite_store import SQLiteStore
from ads_analytics_v2.transport import MemoryBroker, consume_messages, publish_once
from tests.helpers import CUTOFF, event, test_directory


class StorageTransportTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.addCleanup(self.store.close)

    def test_atomic_ingest_rolls_back_entire_conflicting_batch(self):
        self.store.ingest([event()], enqueue=True)
        with self.assertRaises(Conflict):
            self.store.ingest([event("new", delivery="new"), replace(event(), ad_id="changed")], enqueue=True)
        self.assertEqual(len(self.store.read_events()), 1)
        self.assertEqual(self.store.queue_counts(), {"PENDING": 1})

    def test_reingest_preserves_sent_state(self):
        self.store.ingest([event()], enqueue=True)
        publish_once(self.store, MemoryBroker(), clock=lambda: 0)
        self.assertEqual(self.store.ingest([event()], enqueue=True), 0)
        self.assertEqual(self.store.queue_counts(), {"SENT": 1})

    def test_stale_ack_cannot_complete_reclaimed_lease(self):
        self.store.ingest([event()], enqueue=True)
        first = self.store.claim(now=0, lease_seconds=10)
        replacement = self.store.claim(now=10, lease_seconds=10)
        self.assertNotEqual(first.lease_token, replacement.lease_token)
        with self.assertRaises(LeaseLost):
            self.store.acknowledge(first, now=11)
        self.store.acknowledge(replacement, now=11)
        self.assertEqual(self.store.queue_counts(), {"SENT": 1})

    def test_crash_after_broker_ack_replays_without_double_counting(self):
        self.store.ingest([event()], enqueue=True)
        broker = MemoryBroker()
        first = self.store.claim(now=0, lease_seconds=10)
        broker.publish(key=first.event.delivery_id.encode(), value=json.dumps(first.event.envelope()).encode())
        # Simulate process death: no local ACK; another publisher reclaims the expired lease.
        result = publish_once(self.store, broker, clock=lambda: 11)
        self.assertEqual(result["sent"], 1)
        sink = SQLiteStore(":memory:")
        self.addCleanup(sink.close)
        self.assertEqual(consume_messages(sink, broker.messages), 1)
        self.assertEqual(len(broker.messages), 2)
        self.assertEqual(report(sink.read_events(), cutoff=CUTOFF)["totals"]["impressions"], 1)

    def test_publish_failure_delays_retry_and_does_not_mark_sent(self):
        self.store.ingest([event()], enqueue=True)
        class BrokenBroker:
            def publish(self, **kwargs):
                raise OSError("do-not-echo-connection-details")
        self.assertEqual(publish_once(self.store, BrokenBroker(), clock=lambda: 0)["failed"], 1)
        self.assertEqual(self.store.queue_counts(), {"PENDING": 1})
        self.assertEqual(publish_once(self.store, MemoryBroker(), clock=lambda: 1)["claimed"], 0)
        self.assertEqual(publish_once(self.store, MemoryBroker(), clock=lambda: 2)["sent"], 1)

    def test_max_attempts_quarantines_queue_item(self):
        self.store.ingest([event()], enqueue=True)
        self.store.claim(now=0, lease_seconds=1, max_attempts=1)
        self.assertIsNone(self.store.claim(now=1, lease_seconds=1, max_attempts=1))
        self.assertEqual(self.store.queue_counts(), {"DEAD": 1})

    def test_concurrent_publishers_claim_only_once(self):
        with test_directory() as directory:
            path = Path(directory) / "v2.sqlite3"
            initial = SQLiteStore(path)
            initial.ingest([event()], enqueue=True)
            initial.close()
            def claim(_):
                store = SQLiteStore(path)
                try:
                    return store.claim(now=0)
                finally:
                    store.close()
            with ThreadPoolExecutor(max_workers=4) as workers:
                claims = list(workers.map(claim, range(4)))
            self.assertEqual(sum(item is not None for item in claims), 1)

    def test_invalid_transport_key_does_not_write_sink(self):
        body = json.dumps(event().envelope()).encode()
        with self.assertRaises(AnalyticsError):
            consume_messages(self.store, [(b"wrong", body)])
        self.assertEqual(self.store.read_events(), [])

    def test_conflicting_transport_batch_is_atomic(self):
        broker = MemoryBroker()
        for item in (event(), replace(event(), ad_id="changed")):
            broker.publish(key=item.delivery_id.encode(), value=json.dumps(item.envelope()).encode())
        with self.assertRaises(Conflict):
            consume_messages(self.store, broker.messages)
        self.assertEqual(self.store.read_events(), [])

    def test_capacity_limit_and_immutable_snapshot(self):
        self.store.ingest([event(), event("other", delivery="other")])
        with self.assertRaises(CapacityExceeded):
            self.store.read_events(limit=1)
        result = report(self.store.read_events(), cutoff=CUTOFF)
        self.store.save_report(result)
        self.store.save_report(result)
        with self.assertRaises(Conflict):
            self.store.save_report({**result, "status": "invented"})

    def test_kafka_adapter_waits_for_ack(self):
        calls = []
        class Future:
            def get(self, *, timeout):
                calls.append(("ack", timeout))
                return "confirmed"
        class Producer:
            def send(self, topic, *, key, value):
                calls.append(("send", topic, key, value))
                return Future()
        self.assertEqual(KafkaBroker(Producer(), topic="reviewed-topic").publish(key=b"d", value=b"e"), "confirmed")
        self.assertEqual(calls[-1], ("ack", 10))


class ApiTests(unittest.TestCase):
    TOKEN = "local-test-token-0123456789"

    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.ingest([event(), event("c", kind="CLICK")])
        self.app = application(self.store, token=self.TOKEN)

    def request(self, path="/reports", *, query=None, auth=True, method="GET"):
        response = {}
        environ = {"PATH_INFO": path, "REQUEST_METHOD": method,
                   "QUERY_STRING": query if query is not None else urlencode({"cutoff": CUTOFF}),
                   "HTTP_AUTHORIZATION": "Bearer " + self.TOKEN if auth else ""}
        def start(status, headers):
            response["status"] = status
        body = b"".join(self.app(environ, start))
        return response["status"], json.loads(body)

    def test_health_is_not_external_service_verification(self):
        status, body = self.request("/health", auth=False)
        self.assertEqual(status, "200 OK")
        self.assertFalse(body["external_services_verified"])

    def test_unauthorized_and_write_methods_are_rejected(self):
        self.assertEqual(self.request(auth=False)[0], "401 Unauthorized")
        self.assertEqual(self.request(method="POST")[0], "405 Method Not Allowed")

    def test_read_report_has_no_storage_side_effect(self):
        before = self.store.connection.total_changes
        status, body = self.request()
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["totals"]["ctr_percent"], 100)
        self.assertEqual(self.store.connection.total_changes, before)

    def test_unknown_duplicate_missing_or_invalid_query_is_rejected(self):
        for query in ("", "cutoff=", "cutoff=bad", "cutoff=x&cutoff=y",
                      urlencode({"cutoff": CUTOFF, "include_private": "1"})):
            with self.subTest(query=query):
                self.assertEqual(self.request(query=query)[0], "400 Bad Request")

    def test_missing_local_token_fails_closed(self):
        with self.assertRaises(AnalyticsError):
            application(self.store, token=None)

    def test_storage_failure_does_not_echo_exception(self):
        with patch.object(self.store, "read_events", side_effect=OSError("private-uri-and-password")):
            status, body = self.request()
        self.assertEqual(status, "503 Service Unavailable")
        self.assertNotIn("private", str(body))


class CliTests(unittest.TestCase):
    def test_storage_cannot_target_v1_or_parent_database(self):
        with self.assertRaises(AnalyticsError):
            local_path("../../backend/local-demo.sqlite3")

    def test_missing_dataset_does_not_create_empty_success_report(self):
        with io.StringIO() as output, patch("sys.stderr", output):
            result = main(["report", "--db", ".local/does-not-exist-test.sqlite3", "--cutoff", CUTOFF])
        self.assertEqual(result, 2)

    def test_fixture_demo_pipeline_expected_metrics(self):
        from ads_analytics_v2 import cli
        original = cli.ROOT
        with test_directory() as directory:
            root = Path(directory)
            (root / "fixtures").mkdir()
            (root / "fixtures/source-events.json").write_bytes((original / "fixtures/source-events.json").read_bytes())
            with patch.object(cli, "ROOT", root):
                result = cli.demo(".local/demo")
            self.assertEqual(result["source_events"], 8)
            self.assertEqual(result["transport_messages_including_replay"], 9)
            self.assertEqual(result["unique_sink_events"], 8)
            self.assertEqual(result["queue"], {"SENT": 8})
            self.assertEqual(result["result_totals"], {
                "impressions": 2, "matched_clicks": 1, "early_clicks": 1,
                "unmatched_clicks": 1, "pending_clicks": 1, "ctr_percent": 50.0})
