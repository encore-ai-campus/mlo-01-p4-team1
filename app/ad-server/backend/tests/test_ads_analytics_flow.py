"""Offline integration: source capture -> broker -> sink snapshot -> authenticated API."""

import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ads_analytics_v2.events import timestamp
from ads_analytics_v2.pipeline import run_once
from ads_analytics_v2.sqlite_store import SQLiteStore
from ads_analytics_v2.transport import MemoryBroker
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

CUTOFF = "2026-11-06T01:10:00Z"


class Cursor(list):
    def sort(self, name, order=1):
        return Cursor(sorted(self, key=lambda row: row[name], reverse=order == -1))

    def limit(self, value):
        return Cursor(self[:value])


class Source:
    name = "ad_events"

    def __init__(self, rows):
        self.rows = deepcopy(rows)

    def find(self, query, projection):
        result = []
        for row in self.rows:
            if timestamp(row["received_at"]) > query["received_at"]["$lte"]:
                continue
            bounds = query.get("event_id", {})
            if "$gt" in bounds and row["event_id"] <= bounds["$gt"]:
                continue
            if "$lte" in bounds and row["event_id"] > bounds["$lte"]:
                continue
            result.append(
                {key: row[key] for key, include in projection.items() if include and key in row}
            )
        return Cursor(result)


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ADS_V2_ENABLED=True,
    ADS_ADMIN_AUTHORIZER="tests.test_ads_api.allowed_admin",
    ADS_V2_INTERVAL_SECONDS=300,
    ADS_V2_STALE_SECONDS=600,
)
class AnalyticsFlowTests(SimpleTestCase):
    def setUp(self):
        app = Path(__file__).resolve().parents[3]
        self.temp = tempfile.TemporaryDirectory(dir=self.local(app))
        self.addCleanup(self.temp.cleanup)
        self.producer = SQLiteStore(Path(self.temp.name) / "producer.sqlite3")
        self.sink = SQLiteStore(Path(self.temp.name) / "sink.sqlite3")
        self.addCleanup(self.producer.close)
        self.addCleanup(self.sink.close)
        rows = json.loads(
            (app / "prototype/ads-analytics-v2/fixtures/source-events.json").read_text()
        )
        self.source = Source(rows)
        self.before = deepcopy(self.source.rows)
        self.broker = MemoryBroker()

    @staticmethod
    def local(app):
        root = app / ".local/ads-flow-tests"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def cycle(self, consume=True):
        return run_once(
            self.source,
            self.producer,
            self.broker,
            self.sink,
            source_id="offline-http",
            media_id="synthetic-media",
            cutoff=CUTOFF,
            clock=lambda: 1000,
            page_size=100,
            publish_limit=100,
            consumer=(lambda: list(self.broker.messages)) if consume else None,
        )

    def response(self):
        with (
            patch(
                "api.ads_analytics.latest_snapshot",
                side_effect=lambda cutoff: self.sink.latest_report(cutoff=cutoff),
            ),
            patch("api.ads_analytics.timezone.now", return_value=timestamp(CUTOFF)),
        ):
            return APIClient().get(
                "/api/admin/ads/analytics?date_from=2026-11-05&date_to=2026-11-06"
            )

    def test_collected_published_consumed_snapshot_is_the_report_visible_in_admin_api(self):
        result = self.cycle()
        self.assertEqual(result["capture"]["source_rows"], 8)
        self.assertEqual(result["publish"]["sent"], 8)
        self.assertEqual(result["consume"]["inserted"], 8)
        response = self.response()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["report_available"])
        self.assertEqual(data["report_id"], result["consume"]["report"]["report_id"])
        self.assertEqual(data["totals"]["impressions"], 2)
        self.assertEqual(data["totals"]["matched_clicks"], 1)
        self.assertEqual(data["totals"]["ctr_percent"], 50.0)
        self.assertEqual(data["totals"]["early_clicks"], 1)
        # No operator settlement is asserted: orphan clicks remain pending.
        self.assertEqual(data["totals"]["pending_clicks"], 2)
        self.assertFalse(data["policy_confirmed"])
        self.assertEqual(self.source.rows, self.before)
        replay = self.cycle()
        self.assertEqual(replay["consume"]["inserted"], 0)
        self.assertEqual(self.response().json()["totals"], data["totals"])

    def test_broker_ack_without_consumption_does_not_expose_completed_report(self):
        self.cycle(consume=False)
        self.assertEqual(self.producer.queue_counts(), {"SENT": 8})
        waiting = self.response().json()
        self.assertFalse(waiting["report_available"])
        self.assertEqual(waiting["status"], "PENDING")
        self.assertIsNone(waiting["totals"]["impressions"])
