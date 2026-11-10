import copy
import unittest
from dataclasses import replace

from ads_analytics_v2.errors import AnalyticsError, CapacityExceeded, Conflict
from ads_analytics_v2.events import EVENT_SCHEMA, Event, stamp, timestamp
from ads_analytics_v2.reporting import report
from tests.helpers import CUTOFF, SETTLED, event


class EventTests(unittest.TestCase):
    def test_v1_mapping_whitelists_private_fields_and_preserves_source(self):
        source = event().envelope()
        source["ads_policy_version"] = source.pop("source_policy_version")
        source.pop("event_schema_version")
        source.pop("media_id")
        source.update(owner_guest_id="private-guest", delivery_token="private-token", estimated_tokens=8)
        before = copy.deepcopy(source)
        parsed = Event.from_v1_record(source, media_id="registered-media")
        self.assertEqual(source, before)
        self.assertNotIn("private", str(parsed.envelope()))
        self.assertNotIn("estimated_tokens", parsed.envelope())
        self.assertEqual(parsed.media_id, "registered-media")

    def test_envelope_round_trip(self):
        self.assertEqual(Event.from_envelope(event().envelope()), event())

    def test_unknown_envelope_fields_are_rejected(self):
        with self.assertRaisesRegex(AnalyticsError, "INVALID_EVENT_FIELDS"):
            Event.from_envelope({**event().envelope(), "delivery_token": "secret"})

    def test_equivalent_offsets_have_identical_fingerprints(self):
        same = replace(event(), occurred_at="2026-11-06T09:00:00+09:00",
                       received_at="2026-11-06T09:00:00+09:00")
        self.assertEqual(same.fingerprint, event().fingerprint)

    def test_naive_time_is_rejected(self):
        with self.assertRaisesRegex(AnalyticsError, "INVALID_TIMESTAMP"):
            event(occurred="2026-11-06T00:00:00")

    def test_future_counted_event_is_rejected(self):
        with self.assertRaisesRegex(AnalyticsError, "INVALID_COUNTED_EVENT_TIME"):
            event(occurred="2026-11-06T00:01:00Z", received="2026-11-06T00:00:00Z")

    def test_source_five_minute_boundary(self):
        event(received="2026-11-06T00:05:00Z")
        with self.assertRaisesRegex(AnalyticsError, "INVALID_COUNTED_EVENT_TIME"):
            event(received="2026-11-06T00:05:00.001Z")

    def test_conflicting_counted_flag_and_test_count_are_rejected(self):
        for overrides in ({"counted": False}, {"is_test": True}, {"counted": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(AnalyticsError):
                event(**overrides)

    def test_unsupported_schema_and_missing_v1_fields(self):
        with self.assertRaises(AnalyticsError):
            event(event_schema_version=EVENT_SCHEMA + "-unknown")
        with self.assertRaises(AnalyticsError):
            Event.from_v1_record({}, media_id="x")


class ReportTests(unittest.TestCase):
    def run_report(self, events, **options):
        return report(events, cutoff=CUTOFF, **options)

    def test_normal_link_and_ctr(self):
        result = self.run_report([event(), event("c", kind="CLICK")])
        self.assertEqual(result["totals"]["impressions"], 1)
        self.assertEqual(result["totals"]["matched_clicks"], 1)
        self.assertEqual(result["totals"]["ctr_percent"], 100)

    def test_click_only_is_pending_and_does_not_fabricate_impression(self):
        result = self.run_report([event("c", kind="CLICK")])
        self.assertEqual(result["totals"]["pending_clicks"], 1)
        self.assertEqual(result["totals"]["impressions"], 0)
        self.assertIsNone(result["totals"]["ctr_percent"])
        self.assertEqual(result["status"], "PROVISIONAL")

    def test_explicit_settlement_classifies_unmatched_without_altering_source(self):
        click = event("c", kind="CLICK")
        result = self.run_report([click], settled_through=SETTLED)
        self.assertEqual(result["totals"]["unmatched_clicks"], 1)
        self.assertEqual(result["status"], "PROVISIONAL")
        self.assertEqual(result["classification_boundary"], "CALLER_SUPPLIED")
        self.assertIn("NOT_VERIFIED", result["settlement_note"])
        self.assertTrue(click.counted)

    def test_early_click_does_not_join_future_impression(self):
        result = self.run_report([
            event("c", kind="CLICK"), event(occurred="2026-11-06T00:00:01Z")])
        self.assertEqual(result["totals"]["early_clicks"], 1)
        self.assertEqual(result["totals"]["matched_clicks"], 0)

    def test_receipt_order_is_not_occurrence_order(self):
        result = self.run_report([
            event("c", kind="CLICK", occurred="2026-11-06T00:00:01Z"),
            event(received="2026-11-06T00:00:20Z")])
        self.assertEqual(result["totals"]["matched_clicks"], 1)

    def test_as_of_does_not_include_later_receipt_and_old_report_is_unchanged(self):
        events = [event("c", kind="CLICK", occurred="2026-11-06T00:00:01Z"),
                  event(received="2026-11-06T00:00:20Z")]
        earlier = report(events, cutoff="2026-11-06T00:00:10Z")
        before = copy.deepcopy(earlier)
        later = self.run_report(events)
        self.assertEqual(earlier["totals"]["pending_clicks"], 1)
        self.assertEqual(later["totals"]["matched_clicks"], 1)
        self.assertNotEqual(earlier["report_id"], later["report_id"])
        self.assertEqual(earlier, before)

    def test_cross_midnight_click_is_attributed_to_impression_day(self):
        events = [event(occurred="2026-11-05T14:59:59Z"),
                  event("c", kind="CLICK", occurred="2026-11-05T15:00:01Z")]
        previous_day = self.run_report(events, date_from="2026-11-05", date_to="2026-11-05")
        next_day = self.run_report(events, date_from="2026-11-06", date_to="2026-11-06")
        self.assertEqual(previous_day["totals"]["matched_clicks"], 1)
        self.assertEqual(next_day["totals"]["matched_clicks"], 0)
        self.assertEqual(previous_day["rows"][0]["date_kst"], "2026-11-05")

    def test_identical_event_replay_is_deduplicated(self):
        self.assertEqual(self.run_report([event(), event()])["totals"]["impressions"], 1)

    def test_conflicting_event_id_is_not_silently_counted(self):
        with self.assertRaisesRegex(Conflict, "EVENT_ID_CONFLICT"):
            self.run_report([event(), replace(event(), placement="bottom")])

    def test_conflicting_delivery_or_multiple_counted_type_is_rejected(self):
        cases = [[event(), event("c", kind="CLICK", creative_version="other")],
                 [event(), event("i2")]]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(Conflict):
                self.run_report(events)

    def test_excluded_and_test_events_do_not_change_performance(self):
        events = [event(), event("test", is_test=True, result="MEASUREMENT_UNAVAILABLE", counted=False),
                  event("repeat", result="REPEAT_SUPPRESSED", counted=False)]
        result = self.run_report(events)
        self.assertEqual(result["totals"]["impressions"], 1)
        self.assertEqual(result["diagnostics"]["test_events_excluded"], 1)

    def test_total_ctr_is_weighted_from_counts_not_mean_of_rates(self):
        events = [event(f"i{n}", delivery=f"d{n}") for n in range(10)]
        events += [event("c0", delivery="d0", kind="CLICK"),
                   event("ib", delivery="db", ad_id="other-ad"),
                   event("cb", delivery="db", ad_id="other-ad", kind="CLICK")]
        result = self.run_report(events)
        self.assertEqual(result["totals"]["ctr_percent"], round(200 / 11, 6))

    def test_scope_filters_media_ad_and_placement(self):
        result = self.run_report([event(), event("i2", delivery="d2", media_id="other")],
                                 media_id="muther-test", ad_id="ad-one", placement="top")
        self.assertEqual(result["totals"]["impressions"], 1)

    def test_invalid_period_and_unknown_filters(self):
        for scope in ({"date_from": "2026-11-01"}, {"date_from": "2026-11-02", "date_to": "2026-11-01"},
                      {"date_from": "2020-01-01", "date_to": "2026-01-01"},
                      {"placement": "sidebar"}, {"unknown": True}):
            with self.subTest(scope=scope), self.assertRaises(AnalyticsError):
                self.run_report([], **scope)

    def test_settlement_cannot_be_after_cutoff(self):
        with self.assertRaises(AnalyticsError):
            self.run_report([], settled_through="2027-01-01T00:00:00Z")

    def test_capacity_limit_fails_instead_of_returning_partial_totals(self):
        with self.assertRaises(CapacityExceeded):
            self.run_report([event(), event("i2", delivery="d2")], max_events=1)

    def test_empty_report_is_not_a_claim_of_external_readiness(self):
        result = self.run_report([])
        self.assertIsNone(result["totals"]["ctr_percent"])
        self.assertEqual(result["status"], "PROVISIONAL")

    def test_reordered_input_has_same_report_id(self):
        events = [event(), event("c", kind="CLICK")]
        self.assertEqual(self.run_report(events)["report_id"], self.run_report(reversed(events))["report_id"])

    def test_new_click_remains_pending_after_older_settlement(self):
        result = self.run_report([event("c", kind="CLICK", occurred="2026-11-06T01:09:00Z")],
                                 settled_through=SETTLED)
        self.assertEqual(result["totals"]["pending_clicks"], 1)

    def test_timestamp_serialization_is_utc(self):
        self.assertEqual(stamp(timestamp("2026-11-06T09:00:00+09:00")), "2026-11-06T00:00:00.000000Z")
