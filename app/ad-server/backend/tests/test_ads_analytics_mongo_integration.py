"""Opt-in V2 transactions on explicit localhost ads-rs and UUID-only disposable DBs.

Kafka is deliberately a memory broker here. Only Mongo storage and the Django
HTTP read path are real; this suite does not claim Kafka/Spark/AWS integration.
"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import SkipTest
from uuid import uuid4

from ads_analytics_v2.adapters.mongo import MongoStore
from ads_analytics_v2.adapters.source_snapshot import capture_v1_page
from ads_analytics_v2.errors import Conflict
from ads_analytics_v2.events import Event, stamp
from ads_analytics_v2.pipeline import consume_and_materialize
from ads_analytics_v2.reporting import report
from ads_analytics_v2.transport import MemoryBroker, publish_once
from django.test import SimpleTestCase, override_settings
from pymongo import MongoClient
from pymongo.uri_parser import parse_uri
from rest_framework.test import APIClient

from tests.test_ads_mongo_integration import INTEGRATION_DB_PATTERN


def cleanup_databases(client, names):
    # Validate every target before deleting the first; keep the shared client
    # open until all three owned databases have been handled.
    if (
        len(names) != 3
        or len(set(names)) != 3
        or any(not INTEGRATION_DB_PATTERN.fullmatch(name) for name in names)
    ):
        client.close()
        raise RuntimeError("UUID 시험 DB 경계를 확인할 수 없어 정리를 중단합니다.")
    failures = []
    try:
        for name in names:
            try:
                client.drop_database(name)
            except Exception as error:
                failures.append(type(error).__name__)
    finally:
        client.close()
    if failures:
        raise RuntimeError("일부 UUID 시험 DB 정리에 실패했습니다: " + ", ".join(failures))


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ADS_V2_ENABLED=True,
    ADS_ADMIN_AUTHORIZER="tests.test_ads_api.allowed_admin",
    ADS_V2_STALE_SECONDS=600,
    ADS_V2_INTERVAL_SECONDS=300,
)
class AdsAnalyticsMongoIntegrationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if os.environ.get("ADS_RUN_MONGO_TESTS") != "1":
            raise SkipTest("실제 V2 MongoDB 미실행: ADS_RUN_MONGO_TESTS=1이 필요합니다.")
        uri = os.environ.get("ADS_TEST_MONGODB_URI", "")
        if not uri:
            raise SkipTest("ADS_TEST_MONGODB_URI를 명시해야 실제 V2 MongoDB를 실행합니다.")
        if not uri.startswith("mongodb://"):
            raise RuntimeError("명시한 localhost mongodb:// URI만 시험할 수 있습니다.")
        parsed = parse_uri(uri)
        if not parsed["nodelist"] or any(
            host not in {"localhost", "127.0.0.1", "::1"} for host, _ in parsed["nodelist"]
        ):
            raise RuntimeError("실제 V2 통합 시험은 localhost MongoDB에만 연결합니다.")
        cls.mongo_client = MongoClient(
            uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            timeoutMS=15000,
            readPreference="primary",
        )
        try:
            hello = cls.mongo_client.admin.command("hello")
            if hello.get("setName") != "ads-rs" or not hello.get("isWritablePrimary"):
                raise RuntimeError("실제 V2 통합 시험에는 ads-rs primary가 필요합니다.")
            cls.database_names = tuple(f"ads_integration_{uuid4().hex}_dev" for _ in range(3))
            if len(set(cls.database_names)) != 3 or any(
                not INTEGRATION_DB_PATTERN.fullmatch(name) for name in cls.database_names
            ):
                raise RuntimeError("시험 전용 DB 이름을 확인할 수 없습니다.")
        except Exception:
            cls.mongo_client.close()
            raise
        cls.addClassCleanup(cleanup_databases, cls.mongo_client, cls.database_names)
        cls.uri = uri
        cls.original_db, cls.producer_db, cls.sink_db = [
            cls.mongo_client[name] for name in cls.database_names
        ]
        cls.original_db["ad_events"].create_index("event_id", unique=True)
        MongoStore(cls.producer_db).prepare_indexes()
        MongoStore(cls.sink_db).prepare_indexes()

    def setUp(self):
        for database in (self.original_db, self.producer_db, self.sink_db):
            if database.name not in self.database_names or not INTEGRATION_DB_PATTERN.fullmatch(
                database.name
            ):
                raise RuntimeError("시험 데이터 정리의 UUID DB 경계를 확인할 수 없습니다.")
            for name in database.list_collection_names():
                database[name].delete_many({})
        self.now = datetime(2026, 11, 9, 5, tzinfo=UTC)
        self.cutoff = self.now + timedelta(seconds=10)
        self.producer = MongoStore(self.producer_db)
        self.sink = MongoStore(self.sink_db)
        self.source = self.original_db["ad_events"]

    def source_rows(self):
        values = {
            "delivery_id": "delivery-one",
            "ad_id": "ad-one",
            "creative_version": "creative-one",
            "placement": "top",
            "result": "COUNTED",
            "counted": True,
            "is_test": False,
            "ads_policy_version": "synthetic-integration-policy",
        }
        return [
            {
                **values,
                "_id": "event-001",
                "event_id": "event-001",
                "event_type": "VIEWABLE_IMPRESSION",
                "occurred_at": self.now,
                "received_at": self.now + timedelta(seconds=1),
            },
            {
                **values,
                "_id": "event-002",
                "event_id": "event-002",
                "event_type": "CLICK",
                "occurred_at": self.now + timedelta(seconds=2),
                "received_at": self.now + timedelta(seconds=3),
            },
        ]

    def events(self):
        return [Event.from_v1_record(row, media_id="synthetic-media") for row in self.source_rows()]

    def captured_messages(self):
        self.source.insert_many(self.source_rows())
        original = list(self.source.find().sort("_id", 1))
        pages = []
        for _ in range(3):
            page = capture_v1_page(
                self.source,
                self.producer,
                source_id="integration-source",
                media_id="synthetic-media",
                cutoff=self.cutoff,
                page_size=1,
                now=100,
            )
            pages.append(page)
            if page["sweep_complete"]:
                break
        self.assertEqual(len(pages), 2)
        self.assertEqual([page["inserted"] for page in pages], [1, 1])
        self.assertEqual(self.producer.capture_state("integration-source")["version"], 2)
        self.assertEqual(self.producer.outbox.count_documents({"state": "PENDING"}), 2)
        broker = MemoryBroker()
        counts = publish_once(self.producer, broker, clock=lambda: 101, limit=10)
        self.assertEqual(counts["sent"], 2)
        self.assertEqual(self.producer.queue_counts(), {"SENT": 2})
        self.assertEqual(list(self.source.find().sort("_id", 1)), original)
        return broker.messages

    def test_source_outbox_distinct_sink_materialized_then_admin_http(self):
        messages = self.captured_messages()
        outcome = consume_and_materialize(self.sink, messages, cutoff=self.cutoff, batch_size=1)
        self.assertEqual(outcome["inserted"], 2)
        self.assertNotEqual(self.producer.storage_identity, self.sink.storage_identity)
        self.assertEqual(self.producer.reports.count_documents({}), 0)
        with override_settings(
            ADS_V2_SINK_MONGODB_URI=self.uri, ADS_V2_SINK_MONGODB_DATABASE=self.sink_db.name
        ):
            response = APIClient().get("/api/admin/ads/analytics", {"cutoff": stamp(self.cutoff)})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertTrue(data["report_available"])
        self.assertEqual(data["report_id"], outcome["report"]["report_id"])
        self.assertEqual(data["totals"]["impressions"], 1)
        self.assertEqual(data["totals"]["matched_clicks"], 1)
        self.assertEqual(data["totals"]["ctr_percent"], 100)
        self.assertFalse(data["policy_confirmed"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_materialized_microsecond_cutoff_survives_bson_metadata_precision(self):
        first = report(self.events(), cutoff=self.cutoff.replace(microsecond=100))
        later = report(self.events(), cutoff=self.cutoff.replace(microsecond=900))
        self.sink.save_report(later)
        self.sink.save_report(first)
        bound = self.cutoff.replace(microsecond=300)
        self.assertEqual(self.sink.latest_report(bound)["report_id"], first["report_id"])
        self.assertEqual(self.sink.latest_report()["report_id"], later["report_id"])
        self.assertIsNone(self.sink.latest_report(self.cutoff.replace(microsecond=10)))
        self.assertEqual(
            self.sink.reports.find_one({"_id": later["report_id"]})["report_as_of"].microsecond, 0
        )

    def test_legacy_bson_microsecond_metadata_never_overrides_exact_body_cutoff(self):
        first = report(self.events(), cutoff=self.cutoff.replace(microsecond=100))
        later = report(self.events(), cutoff=self.cutoff.replace(microsecond=900))
        self.sink.reports.insert_many(
            [
                {
                    "_id": value["report_id"],
                    "body": value,
                    "report_as_of": self.cutoff,
                    "saved_at_ns": position,
                }
                for position, value in enumerate((first, later), 1)
            ]
        )
        before = list(self.sink.reports.find().sort("_id", 1))
        self.assertEqual(
            self.sink.latest_report(self.cutoff.replace(microsecond=300))["report_id"],
            first["report_id"],
        )
        self.assertEqual(self.sink.latest_report()["report_id"], later["report_id"])
        self.assertEqual(list(self.sink.reports.find().sort("_id", 1)), before)

    def test_event_conflict_rolls_back_preceding_event_and_outbox_in_same_transaction(self):
        first, second = self.events()
        self.producer.ingest([first], enqueue=True, now=100)
        with self.assertRaises(Conflict):
            self.producer.ingest(
                [second, replace(first, ad_id="conflicting-ad")], enqueue=True, now=101
            )
        self.assertEqual([row["_id"] for row in self.producer.events.find()], [first.event_id])
        self.assertEqual([row["_id"] for row in self.producer.outbox.find()], [first.event_id])
        self.assertEqual(
            self.producer.events.find_one({"_id": first.event_id})["fingerprint"], first.fingerprint
        )

    def test_stale_capture_cas_does_not_commit_new_events_or_progress(self):
        first, second = self.events()
        self.producer.ingest_capture(
            [first], source_id="cas-source", expected_version=0, state={"page": 1}, now=100
        )
        before = self.producer.capture_state("cas-source")
        with self.assertRaises(Conflict):
            self.producer.ingest_capture(
                [second], source_id="cas-source", expected_version=0, state={"page": 2}, now=101
            )
        self.assertEqual(self.producer.capture_state("cas-source"), before)
        self.assertIsNone(self.producer.events.find_one({"_id": second.event_id}))
        self.assertIsNone(self.producer.outbox.find_one({"_id": second.event_id}))

    def test_batched_actual_cursor_replay_is_idempotent_and_does_not_duplicate_report(self):
        messages = self.captured_messages()
        first = consume_and_materialize(self.sink, messages, cutoff=self.cutoff, batch_size=1)
        original = list(self.sink.events.find().sort("_id", 1))
        replay = consume_and_materialize(
            self.sink, messages + messages, cutoff=self.cutoff, batch_size=1
        )
        self.assertEqual(replay["inserted"], 0)
        self.assertEqual(replay["report"], first["report"])
        self.assertEqual(self.sink.reports.count_documents({}), 1)
        self.assertEqual(
            [event.event_id for event in self.sink.iter_events(batch_size=1)],
            ["event-001", "event-002"],
        )
        self.assertEqual(list(self.sink.events.find().sort("_id", 1)), original)
