import copy
import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from mlo_runtime.catalog import accept_export, digest
from mlo_runtime.observability import BoundedSpool, sanitize, pressure, archive_spool
from mlo_runtime.operations import UnsafeTransition
from mlo_runtime.projection import ProjectionRunner
from mlo_runtime.queries import (
    QUERIES,
    EXECUTOR_PORTS,
    TRACK2_QUERIES,
    TRACK2_EXECUTOR_PORTS,
    validate_ports,
)
from mlo_runtime.track2 import validate_artifact, validate_edge, checkpoint, writer_tables
from mlo_runtime.jobs import spark_submit


def event(state="COMMITTED", dataset="mlo.pipeline", event_id=None):
    return {
        "@timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": {
            "id": event_id or str(uuid.uuid4()),
            "dataset": dataset,
            "action": "fixture.transport",
            "outcome": "success",
        },
        "service": {"name": "fixture"},
        "host": {"name": "control-a"},
        "cloud": {
            "provider": "aws",
            "region": "ap-northeast-2",
            "account": {"name": "encore1"},
        },
        "pipeline": {
            "name": "fixture",
            "run_id": str(uuid.uuid4()),
            "stage": "transport",
            "state": state,
            "attempt": 1,
            "records_in": 1,
        },
        "trace": {"id": "0123456789abcdef0123456789abcdef"},
    }


class Track2Acceptance(unittest.TestCase):
    def test_legacy_ports_do_not_move_and_proposed_ports_do_not_collide(self):
        self.assertEqual(EXECUTOR_PORTS, dict(zip(QUERIES, (19006, 19007, 19008))))
        validate_ports()
        collision = {**QUERIES, "venues-catalog": (19006, 19011)}
        with self.assertRaises(UnsafeTransition):
            validate_ports(collision, {**EXECUTOR_PORTS, **TRACK2_EXECUTOR_PORTS})
        with self.assertRaises(UnsafeTransition):
            spark_submit(
                "venues-catalog",
                "/opt/mlo/current/apps/venues.py",
                ["spark-01:7077"],
                "10.0.1.10",
            )
        self.assertEqual(
            checkpoint("demo", "generation-2", "venues-catalog"),
            "aws/demo/generation-2/venues-catalog/checkpoint/",
        )

    def test_artifact_hash_schema_path_and_writer_set_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / "venues.py"
            app.write_text("# accepted synthetic entrypoint\n")
            binding = {
                "version": "0.3.0",
                "accepted": True,
                "dataset_version": "fixture-v1",
                "query": "venues-catalog",
                "entrypoint": str(app),
                "files": {str(app): hashlib.sha256(app.read_bytes()).hexdigest()},
                "schema_sha256": "a" * 64,
                "writer_tables": writer_tables("fixture-v1"),
                "mode": "enqueue-active-group",
            }
            validate_artifact(
                binding, query="venues-catalog", entrypoint=app, root=root
            )
            for change in (
                {"accepted": False},
                {"mode": "direct-submit"},
                {"schema_sha256": "unresolved"},
                {"writer_tables": []},
                {"writer_tables": writer_tables("other-dataset")},
                {"dataset_version": "../other-dataset"},
                {"version": "0.2.0"},
                {"files": {str(app): "0" * 64}},
            ):
                with self.subTest(change=change), self.assertRaises(UnsafeTransition):
                    validate_artifact(
                        {**binding, **change},
                        query="venues-catalog",
                        entrypoint=app,
                        root=root,
                    )
            app.write_text("# modified after acceptance")
            with self.assertRaises(UnsafeTransition):
                validate_artifact(
                    binding, query="venues-catalog", entrypoint=app, root=root
                )

    def test_complete_export_late_duplicate_revision_tombstone_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.ndjson"
            rows = [
                {"id": "v1", "revision": 2, "deleted": True, "document": {}},
                {
                    "id": "v1",
                    "revision": 1,
                    "deleted": False,
                    "document": {"name": "old"},
                },
                {
                    "id": "v2",
                    "revision": 1,
                    "deleted": False,
                    "document": {"name": "fixture"},
                },
            ]
            path.write_text(
                "\n".join(json.dumps(row) for row in [*rows, rows[2]]) + "\n"
            )
            selected = [rows[0], rows[2]]
            manifest = {
                "version": "0.2.0",
                "query": "venues-catalog",
                "release_id": "fixture-1",
                "dataset_version": "dataset-1",
                "complete": True,
                "schema_sha256": "a" * 64,
                "files": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
                "records": 2,
                "documents_sha256": digest(selected),
            }
            self.assertEqual(
                accept_export(manifest, directory, expected_schema="a" * 64), selected
            )
            for change in (
                {"complete": False},
                {"records": 3},
                {"documents_sha256": "0" * 64},
                {"files": {"../part.ndjson": "0" * 64}},
            ):
                with self.subTest(change=change), self.assertRaises(UnsafeTransition):
                    accept_export(
                        {**manifest, **change}, directory, expected_schema="a" * 64
                    )
            path.write_text("changed")
            with self.assertRaises(UnsafeTransition):
                accept_export(manifest, directory, expected_schema="a" * 64)

    def test_origin_tls_unresolved_spoofed_protocol_and_insecure_cookie_fail(self):
        binding = {
            "accepted": True,
            "origin_hostname": "origin.example.test",
            "origin_tls_name": "origin.example.test",
            "viewer_origin": "https://fixture.cloudfront.net",
            "rp_id": "fixture.cloudfront.net",
            "origin_certificate_verified": True,
            "control_tls_verified": True,
            "secure_cookies": True,
            "csrf_origin": "https://fixture.cloudfront.net",
            "viewer_protocol_header": "CloudFront-Forwarded-Proto",
            "viewer_protocol": "https",
            "edge_overwrites_viewer_header": True,
            "trusted_proxy_cidrs": ["10.0.0.0/24"],
            "ingress_restricted": True,
        }
        self.assertTrue(validate_edge(binding)["ready"])
        for change in (
            {"accepted": False},
            {"origin_hostname": "fixture.elb.amazonaws.com"},
            {"viewer_protocol_header": "X-Forwarded-Proto"},
            {"secure_cookies": False},
            {"trusted_proxy_cidrs": ["0.0.0.0/0"]},
            {"ingress_restricted": False},
        ):
            with self.subTest(change=change), self.assertRaises(UnsafeTransition):
                validate_edge({**binding, **change})

    def test_viewer_https_demo_is_explicit_and_never_full_tls(self):
        binding = {
            "accepted": True, "transport_profile": "viewer-https-demo",
            "environment": "dev", "demo_only": True, "synthetic_data_only": True,
            "origin_hostname": "p4-control-123.ap-northeast-2.elb.amazonaws.com",
            "origin_protocol": "http", "origin_port": 80,
            "control_protocol": "http", "control_port": 8000,
            "origin_tls_name": "", "origin_certificate_verified": False,
            "control_tls_verified": False,
            "viewer_origin": "https://d123example.cloudfront.net",
            "rp_id": "d123example.cloudfront.net", "secure_cookies": True,
            "csrf_origin": "https://d123example.cloudfront.net",
            "viewer_protocol_header": "CloudFront-Forwarded-Proto",
            "viewer_protocol": "https", "edge_overwrites_viewer_header": True,
            "trusted_proxy_cidrs": ["10.210.1.0/24"], "ingress_restricted": True,
        }
        result = validate_edge(binding)
        self.assertTrue(result["ready"])
        self.assertFalse(result["full_tls"])
        for change in (
            {"transport_profile": "full-tls"}, {"demo_only": False},
            {"environment": "prod"}, {"synthetic_data_only": False},
            {"origin_certificate_verified": True}, {"control_tls_verified": True},
            {"origin_hostname": "unrelated.example.org"}, {"control_port": 8443},
            {"secure_cookies": False}, {"viewer_protocol_header": "X-Forwarded-Proto"},
            {"ingress_restricted": False}, {"trusted_proxy_cidrs": ["0.0.0.0/0"]},
        ):
            with self.subTest(change=change), self.assertRaises(UnsafeTransition):
                validate_edge({**binding, **change})


class Observations(unittest.TestCase):
    def test_secrets_and_pii_removed_and_values_fail_closed(self):
        raw = event()
        raw.update(
            message="secret cookie email user@example.test",
            request={"password": "secret"},
            before={"email": "user@example.test"},
        )
        raw["event"]["original"] = "sensitive request"
        cleaned = sanitize(raw)
        self.assertNotIn("secret", json.dumps(cleaned))
        self.assertNotIn("email", json.dumps(cleaned))
        for group, key, value in [
            ("service", "name", "user@example.test"),
            ("pipeline", "source_ref", "raw/file?token=secret"),
            ("pipeline", "state", "UNKNOWN"),
            ("pipeline", "attempt", True),
        ]:
            bad = copy.deepcopy(raw)
            bad[group][key] = value
            with self.assertRaises(UnsafeTransition):
                sanitize(bad)

    def test_bounded_spool_refuses_growth_without_deleting_or_saving_pii(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(directory, max_bytes=4096, max_files=2)
            emitted = 0
            with self.assertRaises(UnsafeTransition):
                for _ in range(30):
                    spool.append({**event(), "message": "secret"})
                    emitted += 1
            data = b"".join(p.read_bytes() for p in Path(directory).glob("*.ndjson"))
            self.assertGreater(emitted, 0)
            self.assertNotIn(b"secret", data)
            self.assertLessEqual(len(data), 4096)
            self.assertEqual(len(data.splitlines()), emitted)

    def test_queue_pressure_and_silent_progress_are_distinct(self):
        self.assertEqual(pressure(80, 100, 10, now=150)["state"], "STALE")
        self.assertTrue(pressure(80, 100, 149, now=150)["pressure"])
        self.assertEqual(pressure(0, 100, 149, now=150)["state"], "HEARTBEAT")

    def test_sanitized_archive_restores_without_deleting_source(self):
        import gzip

        with tempfile.TemporaryDirectory() as directory:
            spool = BoundedSpool(Path(directory) / "source")
            fixed = event()
            spool.append({**fixed, "message": "secret"})
            archived = archive_spool(spool.folder, Path(directory) / "archive")
            restored = gzip.decompress(Path(archived["path"]).read_bytes())
            self.assertEqual(json.loads(restored)["event"]["id"], fixed["event"]["id"])
            self.assertNotIn(b"secret", restored)
            self.assertEqual(len(list(spool.folder.glob("*.ndjson"))), 1)
            self.assertEqual(
                archived, archive_spool(spool.folder, Path(directory) / "archive")
            )

    def test_projection_cursor_survives_partial_spool_failure_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "cursor.json"
            original = {"log_record": ["immutable"], "budget": 123}
            immutable = copy.deepcopy(original)
            fixed = event(dataset="mlo.ads")
            calls = []

            def exporter(cursor, limit):
                calls.append(cursor)
                return {"events": [fixed, fixed], "cursor": "opaque-2", "backlog": 0}

            class Sink:
                def __init__(self):
                    self.events, self.fail = [], True

                def append(self, value):
                    if self.fail and len(self.events) == 1:
                        raise UnsafeTransition("full")
                    self.events.append(value)

            sink = Sink()
            runner = ProjectionRunner(
                state, sink, exporter, lambda: True, event("HEARTBEAT")
            )
            with self.assertRaises(UnsafeTransition):
                runner.step()
            self.assertFalse(state.exists())
            sink.fail = False
            runner.step()
            self.assertEqual(calls, [None, None])
            self.assertEqual(json.loads(state.read_text())["cursor"], "opaque-2")
            self.assertEqual(original, immutable)
            self.assertEqual(sink.events[0]["event"]["id"], fixed["event"]["id"])


if __name__ == "__main__":
    unittest.main()
