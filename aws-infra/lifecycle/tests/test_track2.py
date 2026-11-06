"""Track2 registry, preservation, replay and generation rejection tests."""

import copy
import unittest

from mlo_lifecycle.model import Refused, digest, validate_bundle
from mlo_lifecycle.planner import plan, validate_plan
from mlo_lifecycle.recovery import outputs
from mlo_lifecycle.track2 import ASSETS, WRITERS
from track2_support import track2_fixture


class Track2Unit(unittest.TestCase):
    def test_every_writer_and_asset_registered_and_backup_ordered_after_seal(self):
        bundle = track2_fixture()
        validate_bundle(bundle)
        reviewed = plan(bundle)
        steps = {s["id"]: s for s in reviewed["steps"]}
        for writer in (*WRITERS, *bundle["track2"]["app_workers"]):
            self.assertIn("track2:drain:" + writer, steps)
        self.assertEqual(set(bundle["track2"]["assets"]), ASSETS)
        self.assertEqual(steps["idle"]["requires"], ["track2:freeze"])
        self.assertEqual(steps["stateful:postgresql"]["requires"], ["track2:seal"])
        for step in steps.values():
            if step["action"] == "delete":
                self.assertIn("track2:seal", step["requires"])

    def test_each_writer_and_each_preserved_asset_omission_rejected(self):
        for field in ("writers", "assets"):
            for name in track2_fixture()["track2"][field]:
                bundle = track2_fixture()
                del bundle["track2"][field][name]
                with self.subTest(field=field, name=name), self.assertRaises(Refused):
                    plan(bundle)

    def test_invalid_versions_generations_cursors_queues_and_partial_release(self):
        mutations = [
            ("checkpoint-venues-catalog", lambda p: p.update(generation="old-generation")),
            ("checkpoint-venues-catalog", lambda p: p.update(path="aws/dev/shared/checkpoint/")),
            ("raw-manifest", lambda p: p.update(path="raw/venues/other-dataset/")),
            ("lake-manifest", lambda p: p.update(path="lake/venues/other-dataset/v1/")),
            ("exporter-cursor", lambda p: p.update(event_ids=["missing-event"])),
            ("publisher-cursor", lambda p: p.update(generation="old-generation")),
            ("filebeat-queue", lambda p: p.update(max_bytes=0)),
            ("logstash-queue", lambda p: p.update(bytes=8192)),
            ("filebeat-queue", lambda p: p.update(event_ids=["unarchived-event"])),
            ("collector-cursor", lambda p: p.update(archive_sha256="0" * 64)),
            ("releases", lambda p: p["versions"]["release-two"].update(indexed_ids=["venue-a"])),
            ("releases", lambda p: p["versions"]["release-one"].update(manifest_sha256="0" * 64)),
            ("deletions", lambda p: p.clear()),
            ("business-archive", lambda p: p.append({**p[0], "revision": 99})),
        ]
        for name, mutate in mutations:
            bundle = track2_fixture()
            asset = bundle["track2"]["assets"][name]
            mutate(asset["payload"])
            asset["sha256"] = digest(asset["payload"])
            with self.subTest(name=name), self.assertRaises(Refused):
                validate_bundle(bundle)
        bundle = track2_fixture()
        bundle["track2"]["assets"]["pg-ledger"]["version_id"] = ""
        with self.assertRaises(Refused):
            plan(bundle)

    def test_dataset_version_registry_and_old_track2_envelope_rejected(self):
        for change in ("../other-dataset", "data_set", ""):
            bundle = track2_fixture()
            bundle["track2"]["dataset_version"] = change
            with self.subTest(change=change), self.assertRaises(Refused):
                validate_bundle(bundle)
        bundle = track2_fixture()
        bundle["schema_version"] = "lifecycle-track2-input-v1"
        with self.assertRaises(Refused):
            validate_bundle(bundle)

    def test_unapproved_retention_and_purge_disabled_even_with_signed_delete_approval(self):
        bundle = track2_fixture()
        reviewed = plan(bundle, "purge-data")
        self.assertTrue(reviewed["blockers"])
        with self.assertRaises(Refused):
            validate_plan(reviewed, bundle)
        for value in (True, 0):
            changed = copy.deepcopy(bundle)
            changed["track2"]["retention"]["automatic_delete"] = value
            with self.assertRaises(Refused):
                plan(changed)

    def test_replay_deduplicates_reconciles_from_pg_and_reapplies_deletes_to_rollback(self):
        bundle = track2_fixture()
        before = copy.deepcopy(bundle)
        result = outputs(bundle["track2"], rollback=True)
        self.assertEqual(result["business-replay"]["reconciled_event_ids"], ["event-c"])
        self.assertEqual([e["entity_id"] for e in result["business-replay"]["events"]], ["venue-a", "venue-c"])
        self.assertEqual([d["entity_id"] for d in result["alias"]["documents"]], ["venue-a"])
        self.assertEqual(result["observation-replay"]["stage_counts"],
                         {"SENT": 1, "ACKNOWLEDGED": 1, "COMMITTED": 1})
        self.assertFalse(result["checkpoints"]["writers_started"])
        self.assertNotIn("SEARCHABLE", result["observation-replay"]["stage_counts"])
        self.assertEqual(bundle, before)

    def test_fencing_epoch_registry_and_app_hook_rejection(self):
        for mutation in (lambda s: s.update(epoch=7), lambda s: s.update(recovery_generation=s["source_generation"]),
                         lambda s: s["writers"]["mail-worker"].update(hook="real-ssm"),
                         lambda s: s["app_workers"].append("unknown-app-worker")):
            bundle = track2_fixture()
            mutation(bundle["track2"])
            with self.assertRaises(Refused):
                plan(bundle)


if __name__ == "__main__":
    unittest.main()
