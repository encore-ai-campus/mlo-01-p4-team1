import copy
import unittest
from dataclasses import replace

from ads_analytics_v2.audit import reconcile
from ads_analytics_v2.benchmark import run_benchmark
from ads_analytics_v2.errors import CapacityExceeded, Conflict
from ads_analytics_v2.reporting import report
from tests.helpers import CUTOFF, event, test_directory


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.impression = event("i")
        self.click = event("c", kind="CLICK", occurred="2026-11-06T00:00:01Z")
        self.events = [self.impression, self.click]
        self.snapshot = report(self.events, cutoff=CUTOFF)

    def test_all_stages_same_observation_and_replay_are_consistent(self):
        result = reconcile(self.events, self.events + self.events,
                           list(reversed(self.events)), self.snapshot, cutoff=CUTOFF)
        self.assertEqual(result["status"], "CONSISTENT_OBSERVATION")
        self.assertEqual(result["duplicate_inputs"]["staged"], 2)
        self.assertFalse(result["source_completeness_verified"])

    def test_dropped_click_detected_even_if_sink_report_matches_its_own_input(self):
        delivered = [self.impression]
        snapshot = report(delivered, cutoff=CUTOFF)
        result = reconcile(self.events, self.events, delivered, snapshot, cutoff=CUTOFF)
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["missing_delivered"]["count"], 1)
        self.assertFalse(result["snapshot"]["totals_match"])

    def test_wrong_materialized_row_detected_even_when_totals_are_unchanged(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["rows"][0]["ad_id"] = "wrong-ad"
        result = reconcile(self.events, self.events, self.events, snapshot, cutoff=CUTOFF)
        self.assertTrue(result["snapshot"]["totals_match"])
        self.assertFalse(result["snapshot"]["rows_match"])
        self.assertEqual(result["status"], "MISMATCH")

    def test_wrong_observation_or_missing_snapshot_never_claims_consistency(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["as_of"] = "2026-11-06T01:09:00Z"
        result = reconcile(self.events, self.events, self.events, snapshot, cutoff=CUTOFF)
        self.assertFalse(result["snapshot"]["observation_matches"])
        result = reconcile([], [], [], None, cutoff=CUTOFF)
        self.assertEqual(result["status"], "MISMATCH")

    def test_conflicting_stage_payload_detected_and_intra_stage_conflict_rejected(self):
        other = replace(self.click, media_id="different-media")
        result = reconcile(self.events, self.events, [self.impression, other],
                           self.snapshot, cutoff=CUTOFF)
        self.assertEqual(result["fingerprint_conflicts"]["count"], 1)
        with self.assertRaises(Conflict):
            reconcile(self.events, self.events + [other], self.events,
                      self.snapshot, cutoff=CUTOFF)

    def test_same_totals_with_other_event_ids_or_policy_are_not_the_same_snapshot(self):
        new_events = [replace(self.impression, event_id="other-i", delivery_id="other-d"),
                      replace(self.click, event_id="other-c", delivery_id="other-d")]
        result = reconcile(new_events, new_events, new_events, self.snapshot, cutoff=CUTOFF)
        self.assertTrue(result["snapshot"]["rows_match"])
        self.assertTrue(result["snapshot"]["totals_match"])
        self.assertFalse(result["snapshot"]["dataset_matches"])
        self.assertEqual(result["status"], "MISMATCH")
        changed_policy = {**self.snapshot, "report_policy_version": "unrelated-policy"}
        result = reconcile(self.events, self.events, self.events, changed_policy, cutoff=CUTOFF)
        self.assertFalse(result["snapshot"]["policy_matches"])
        self.assertEqual(result["status"], "MISMATCH")

    def test_capacity_error_does_not_return_partial_success(self):
        with self.assertRaises(CapacityExceeded):
            reconcile(self.events, self.events, self.events, self.snapshot,
                      cutoff=CUTOFF, max_events=1)

    def test_transport_export_gap_is_detected_and_missing_evidence_is_explicit(self):
        result = reconcile(self.events, self.events, self.events, self.snapshot,
                           cutoff=CUTOFF, transport_events=[self.impression])
        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["transport_missing"]["count"], 1)
        result = reconcile(self.events, self.events, self.events, self.snapshot, cutoff=CUTOFF)
        self.assertFalse(result["transport_checked"])
        self.assertFalse(result["all_supplied_stages_consistent"])

    def test_offline_benchmark_reopens_stores_recovers_ack_gap_and_deduplicates(self):
        with test_directory() as directory:
            result = run_benchmark(directory / "run", pairs=12, batch_size=5)
        self.assertFalse(result["uses_real_kafka_mongodb_spark"])
        self.assertEqual(result["recovery"]["transport_messages"], 25)
        self.assertEqual(result["recovery"]["initial_sink_inserted"], 24)
        self.assertEqual(result["recovery"]["replay_inserted"], 0)
        self.assertEqual(result["recovery"]["queue"], {"SENT": 24})
        self.assertEqual(result["totals"]["impressions"], 12)
        self.assertEqual(result["totals"]["matched_clicks"], 12)
        self.assertEqual(result["reconciliation"]["status"], "CONSISTENT_OBSERVATION")
        self.assertTrue(result["reconciliation"]["all_supplied_stages_consistent"])
