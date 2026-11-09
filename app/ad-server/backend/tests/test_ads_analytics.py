from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings
from rest_framework.test import APIClient

from api.ads_analytics import response_data
from api.common_bridge import call_common
from domains.ads.errors import DomainError

NOW = datetime(2026, 11, 9, 5, tzinfo=UTC)


def snapshot():
    return {
        "report_id": "stored-report",
        "report_policy_version": "proposal-policy",
        "status": "PROVISIONAL",
        "scope": {},
        "as_of": "2026-11-09T04:59:00Z",
        "rows": [
            row("2026-11-08", "a", "top", "m1", 10, 2),
            row("2026-11-09", "b", "bottom", "m2", 2, 1),
        ],
    }


def row(day, ad, placement, media, impressions, clicks):
    return {
        "date_kst": day,
        "ad_id": ad,
        "placement": placement,
        "media_id": media,
        "impressions": impressions,
        "matched_clicks": clicks,
        "early_clicks": 1,
        "unmatched_clicks": 0,
        "pending_clicks": 1,
        "ctr_percent": -999,
    }


@override_settings(
    ALLOWED_HOSTS=["testserver"],
    ADS_V2_ENABLED=True,
    ADS_ADMIN_AUTHORIZER="tests.test_ads_api.allowed_admin",
    ADS_V2_STALE_SECONDS=600,
    ADS_V2_INTERVAL_SECONDS=300,
)
class AnalyticsApiTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()
        self.reader = patch("api.ads_analytics.latest_snapshot", return_value=snapshot()).start()
        self.addCleanup(patch.stopall)
        patch("api.ads_analytics.timezone.now", return_value=NOW).start()

    def get(self, query=""):
        return self.client.get("/api/admin/ads/analytics" + query)

    def test_materialized_snapshot_weighted_totals_and_no_source_scan(self):
        with patch(
            "ads_analytics_v2.reporting.report",
            side_effect=AssertionError("must not aggregate source"),
        ):
            response = self.get()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["totals"]["ctr_percent"], 25)
        self.assertEqual(data["totals"]["impressions"], 12)
        self.assertEqual(data["report_id"], "stored-report")
        self.assertEqual(data["source_snapshot"]["scope"], {})
        self.assertFalse(data["policy_confirmed"])
        self.assertFalse(data["is_stale"])
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_date_ad_placement_media_filters_recalculate_totals(self):
        response = self.get(
            "?date_from=2026-11-09&date_to=2026-11-09&ad_id=b&placement=bottom&media_id=m2"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["rows"]), 1)
        self.assertEqual(response.json()["totals"]["ctr_percent"], 50)
        self.assertEqual(self.get("?media_id=absent").json()["totals"]["impressions"], 0)
        self.assertIsNone(self.get("?media_id=absent").json()["totals"]["ctr_percent"])

    def test_pending_and_measured_zero_are_distinct(self):
        self.reader.return_value = None
        waiting = self.get().json()
        self.assertFalse(waiting["report_available"])
        self.assertIsNone(waiting["totals"]["impressions"])
        existing = snapshot()
        existing["rows"] = []
        self.reader.return_value = existing
        empty = self.get().json()
        self.assertTrue(empty["report_available"])
        self.assertEqual(empty["totals"]["impressions"], 0)

    @override_settings(ADS_V2_ENABLED=False)
    def test_disabled_does_not_open_mongo(self):
        data = self.get().json()
        self.assertFalse(data["enabled"])
        self.reader.assert_not_called()

    def test_query_validation_precedes_storage(self):
        for query in (
            "?media_id=",
            "?media_id=a&media_id=b",
            "?unknown=x",
            "?date_from=2026-11-09",
            "?cutoff=2026-11-09T00:00:00",
            "?placement=left",
        ):
            with self.subTest(query=query):
                self.assertIn(self.get(query).status_code, (400, 422))
        self.reader.assert_not_called()

    def test_cutoff_forwarded_to_materialized_reader(self):
        self.reader.return_value = None
        self.get("?cutoff=2026-11-08T00:00:00Z")
        self.assertEqual(self.reader.call_args.args[0], datetime(2026, 11, 8, tzinfo=UTC))

    def test_storage_failure_and_filtered_snapshot_fail_closed(self):
        self.reader.side_effect = RuntimeError("mongodb://secret:password@internal")
        response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("password", response.content.decode())
        self.reader.side_effect = None
        value = snapshot()
        value["scope"] = {"media_id": "m1"}
        self.reader.return_value = value
        self.assertEqual(self.get().status_code, 503)

    @override_settings(ADS_ADMIN_AUTHORIZER="tests.test_ads_api.missing_admin")
    def test_unauthenticated_cannot_read(self):
        self.assertEqual(self.get().status_code, 401)
        self.reader.assert_not_called()

    @override_settings(ADS_ADMIN_AUTHORIZER="tests.test_ads_api.user_identity")
    def test_ordinary_user_cannot_read(self):
        self.assertEqual(self.get().status_code, 403)
        self.reader.assert_not_called()

    def test_response_filter_preserves_source_snapshot(self):
        original = snapshot()
        before = deepcopy(original)
        result = response_data(original, NOW, {"media_id": "m1"}, NOW)
        self.assertEqual(original, before)
        self.assertEqual(result["totals"]["ctr_percent"], 20)

    def test_old_snapshot_is_marked_stale(self):
        value = snapshot()
        value["as_of"] = "2026-11-09T04:00:00Z"
        self.reader.return_value = value
        self.assertTrue(self.get().json()["is_stale"])


class DockerBridgeBoundaryTests(SimpleTestCase):
    @override_settings(
        DEBUG=True,
        ADS_COMMON_API_URL="http://api:8001",
        ADS_INTERNAL_SECRET="test-only",
        ADS_COMMON_DEV_HTTP_ORIGINS=["http://api:8001"],
    )
    def test_explicit_docker_origin_is_allowed_only_in_debug(self):
        from tests.test_common_bridge import CommonBridgeTests

        with patch("api.common_bridge.build_opener") as opener:
            opener.return_value.open.return_value = CommonBridgeTests().reply({})
            call_common(RequestFactory().get("/"), "admin")
            self.assertEqual(
                opener.return_value.open.call_args.args[0].full_url,
                "http://api:8001/api/internal/ads-context",
            )
        with override_settings(DEBUG=False), patch("api.common_bridge.build_opener") as opener:
            with self.assertRaises(DomainError):
                call_common(RequestFactory().get("/"), "admin")
            opener.assert_not_called()

    @override_settings(
        DEBUG=True,
        ADS_COMMON_API_URL="http://other:8001",
        ADS_INTERNAL_SECRET="test-only",
        ADS_COMMON_DEV_HTTP_ORIGINS=["http://api:8001"],
    )
    def test_allowlist_does_not_allow_other_origin(self):
        with patch("api.common_bridge.build_opener") as opener:
            with self.assertRaises(DomainError):
                call_common(RequestFactory().get("/"), "admin")
            opener.assert_not_called()
