import unittest
from unittest.mock import MagicMock

from ads_analytics_v2.adapters.mongo import COLLECTIONS, MongoStore
from ads_analytics_v2.adapters.source_snapshot import SOURCE_FIELDS, capture_v1_snapshot
from ads_analytics_v2.errors import AnalyticsError, CapacityExceeded
from ads_analytics_v2.sqlite_store import SQLiteStore
from tests.helpers import CUTOFF, event


def source_row():
    row = event().envelope()
    row["ads_policy_version"] = row.pop("source_policy_version")
    row.pop("media_id")
    row.pop("event_schema_version")
    return row


class SourceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.target = SQLiteStore(":memory:")
        self.addCleanup(self.target.close)
        self.source = MagicMock()
        self.source.name = "ad_events"
        self.source.find.return_value.sort.return_value.limit.return_value = [source_row()]

    def capture(self, **options):
        return capture_v1_snapshot(self.source, self.target, media_id="reviewed-media",
                                   cutoff=CUTOFF, **options)

    def test_capture_replay_does_not_duplicate_and_source_is_read_only(self):
        self.assertEqual(self.capture()["inserted"], 1)
        self.assertEqual(self.capture()["inserted"], 0)
        self.source.update_one.assert_not_called()
        self.source.delete_many.assert_not_called()
        self.assertFalse(self.capture()["source_completeness_verified"])

    def test_source_query_never_requests_guest_or_token_fields(self):
        self.capture()
        projection = self.source.find.call_args.args[1]
        self.assertEqual(set(projection), set(SOURCE_FIELDS) | {"_id"})
        self.assertNotIn("delivery_token", projection)

    def test_capture_capacity_error_does_not_partially_ingest(self):
        self.source.find.return_value.sort.return_value.limit.return_value = [source_row()] * 2
        with self.assertRaises(CapacityExceeded):
            self.capture(max_rows=1)
        self.assertEqual(self.target.read_events(), [])

    def test_unknown_source_collection_is_rejected(self):
        self.source.name = "ad_admin_changes"
        with self.assertRaises(AnalyticsError):
            self.capture()

    def test_invalid_lease_bounds_are_rejected(self):
        self.target.ingest([event()], enqueue=True)
        for options in ({"now": float("nan")}, {"now": 0, "lease_seconds": 0},
                        {"now": 0, "max_attempts": True}, {"now": -1}):
            with self.subTest(options=options), self.assertRaises(AnalyticsError):
                self.target.claim(**options)

    def test_mongo_constructor_selects_only_new_names_without_preparing_indexes(self):
        database = MagicMock()
        MongoStore(database)
        self.assertEqual([call.args[0] for call in database.__getitem__.call_args_list],
                         list(COLLECTIONS.values()))
        database.client.start_session.assert_not_called()
        database.__getitem__.return_value.create_index.assert_not_called()
