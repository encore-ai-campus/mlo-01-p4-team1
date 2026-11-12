"""Review-only shared develop app staging tests; no cloud or Git mutation."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest
import zipfile

from mlo_runtime.contracts import ROOT
from mlo_runtime.native_app import prepare
from mlo_runtime.native_control_stage import stage, verify


WEATHER_FEEDBACK_FIXTURE = "f0b132c029836f604edb16953ca222e46c876d0c"
VENUES_DELTA_FIXTURE = "c3c3d58be96b04e2df1fb8c2af5ec093c6a67d1b"


class NativeAppReview(unittest.TestCase):
    def source_fixture(self, sha=WEATHER_FEEDBACK_FIXTURE):
        # Use fixed Git inputs: the supported two-query snapshot and the later
        # venue pipeline require different native dependency reviews.
        git = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", sha + "^{commit}"],
                             capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(git.returncode, 0, "full shared Git history is required for native source fixtures")
        return git.stdout.strip()

    def test_reviewed_team_git_snapshot_excludes_legacy_wrapper_and_is_repeatable(self):
        ref = self.source_fixture()
        with tempfile.TemporaryDirectory(prefix="mlo-control-lab-review-") as folder:
            root = Path(folder).resolve()
            first = prepare(ROOT, ref, root / "review-1")
            second = prepare(ROOT, ref, root / "review-2")
            self.assertEqual(first["pipeline_zip_sha256"], second["pipeline_zip_sha256"])
            self.assertEqual(first["entrypoints"], second["entrypoints"])
            self.assertEqual(first["pinned_develop_commit"], ref)
            self.assertFalse(first["ready_for_aws"])
            self.assertFalse(first["accepted_for_control"])
            self.assertEqual(first["schema_version"], "native-shared-app-review-v3")
            self.assertTrue(first["feedback_backend_sources_bundled"])
            self.assertIn("unity-catalog-client", first["runtime_feature_scope"]["excluded_unverified"])
            self.assertTrue(first["feedback_requires_offline_python_closure"])
            self.assertEqual(first["native_feedback_python_wheel_lock_sha256"],
                             __import__("hashlib").sha256((ROOT / "aws-infra/runtime/native-feedback-python.lock.json").read_bytes()).hexdigest())
            self.assertGreaterEqual(first["source_file_count"], 200)
            self.assertEqual(first["python_requirements_lock_sha256"],
                             first["source_files"]["backend/requirements.lock"])
            with zipfile.ZipFile(root / "review-1" / "pipeline-reviewed.zip") as source:
                self.assertIn("pipeline/weather/mapping.json", source.namelist())
                self.assertIn("pipeline/weather/spark_job.py", source.namelist())
                self.assertIn("backend/common/events.py", source.namelist())
                self.assertIn("backend/cafe/settings.py", source.namelist())
                self.assertIn("backend/requirements.lock", source.namelist())
                self.assertNotIn("pipeline/weather/submit_spark.py", source.namelist())

    def test_venue_delta_requires_separate_native_dependency_review(self):
        ref = self.source_fixture(VENUES_DELTA_FIXTURE)
        with tempfile.TemporaryDirectory(prefix="mlo-native-venues-review-") as folder:
            output = Path(folder).resolve() / "review"
            with self.assertRaisesRegex(ValueError, "unreviewed Unity Catalog/Delta"):
                prepare(ROOT, ref, output)
            self.assertFalse(output.exists())

    def test_staged_lab_candidate_is_immutable_and_production_disallowed(self):
        ref = self.source_fixture()
        with tempfile.TemporaryDirectory(prefix="mlo-control-lab-staging-") as folder:
            root = Path(folder).resolve()
            source = root / "source"
            prepare(ROOT, ref, source)
            accepted = stage(source, root)
            self.assertTrue(accepted["lab_only"])
            self.assertFalse(accepted["ready_for_aws"])
            self.assertEqual(accepted, verify(source, root))
            self.assertEqual(accepted, stage(source, root))
            zip_file = Path(accepted["pipeline_zip"])
            zip_file.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                verify(source, root)
            with self.assertRaises(ValueError):
                stage(source, root)
            with self.assertRaises(ValueError):
                stage(source, Path("/"))

    def test_staging_rejects_unapproved_dependency_lock_identity(self):
        ref = self.source_fixture()
        with tempfile.TemporaryDirectory(prefix="mlo-control-lab-lock-", dir=None) as folder:
            root = Path(folder).resolve()
            source = root / "source"
            prepare(ROOT, ref, source)
            metadata = source / "manifest.json"
            data = json.loads(metadata.read_text())
            data["python_requirements_lock_sha256"] = "0" * 64
            metadata.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                stage(source, root)

    def test_staging_refuses_modified_review_source(self):
        ref = self.source_fixture()
        with tempfile.TemporaryDirectory(prefix="mlo-control-lab-modified-") as folder:
            root = Path(folder).resolve()
            source = root / "source"
            prepare(ROOT, ref, source)
            archive = source / "pipeline-reviewed.zip"
            archive.write_bytes(b"unreviewed")
            with self.assertRaises(ValueError):
                stage(source, root)


if __name__ == "__main__":
    unittest.main()
