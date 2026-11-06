import hashlib
import json
import time
import unittest

from mlo_runtime.catalog import CatalogRelease, accept_export, digest
from mlo_runtime.contracts import RUNTIME
from mlo_runtime.lab import Lab
from mlo_runtime.operations import UnsafeTransition
from track2_lab import elastic_lab, service, start_es


class CatalogAcceptance(unittest.TestCase):
    def test_actual_spark_delta_export_partial_es_publish_rollback_and_fencing(self):
        # Spark is run and stopped before ES starts to bound shared Docker RAM.
        spark = Lab("catalog-spark")
        self.addCleanup(spark.close)
        raw = spark.folder / "raw"
        raw.mkdir()
        first = [
            {"id": "v1", "revision": 2, "deleted": True, "label": None},
            {"id": "v2", "revision": 1, "deleted": False, "label": "original"},
        ]
        (raw / "immutable-1.json").write_text(
            "\n".join(json.dumps(row) for row in first) + "\n"
        )
        first_hash = hashlib.sha256((raw / "immutable-1.json").read_bytes()).hexdigest()
        spark.configure(
            {
                "image": spark.image("spark"),
                "user": "0:0",
                "mem_limit": "3g",
                "volumes": [
                    "data:/data",
                    f"{raw}:/data/raw:ro",
                    f"{RUNTIME / '.artifacts/jars'}:/jars:ro",
                    f"{RUNTIME / 'tests/integration/catalog_fixture.py'}:/test/catalog_fixture.py:ro",
                ],
                "entrypoint": ["sleep", "infinity"],
            }
        )
        spark.up()
        jars = json.loads((RUNTIME / "connectors.lock.json").read_text())["jars"]
        command = [
            "/opt/spark/bin/spark-submit",
            "--master",
            "local[1]",
            "--driver-memory",
            "1g",
            "--jars",
            ",".join("/jars/" + jar["artifact"] for jar in jars),
            "/test/catalog_fixture.py",
        ]
        result = spark.exec(*command, check=False, timeout=180)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"B_CATALOG_PARTIAL_SINK_FAILURE", result.stdout + result.stderr)
        late = [
            {"id": "v1", "revision": 1, "deleted": False, "label": "delayed-deleted"},
            {"id": "v2", "revision": 2, "deleted": False, "label": "updated"},
            {"id": "v3", "revision": 1, "deleted": False, "label": "new"},
        ]
        (raw / "immutable-2.json").write_text(
            "\n".join(json.dumps(row) for row in [*late, late[1]]) + "\n"
        )
        spark.recreate()
        self.assertIn(
            b"B_CATALOG_DELTA_RESTART_DUPLICATE_LATE_DELETE_PASS",
            spark.exec(*command, timeout=180).stdout,
        )
        self.assertEqual(
            hashlib.sha256((raw / "immutable-1.json").read_bytes()).hexdigest(),
            first_hash,
        )
        export = spark.folder / "accepted-export"
        spark.compose("cp", "catalog-spark:/data/export", str(export))
        spark.compose("stop")
        manifest = json.loads((export / "manifest.json").read_text())
        schema = {"version": "catalog-search/1", "properties": {"label": {"type": "keyword"}}}
        documents = accept_export(manifest, export, expected_schema=digest(schema))
        lab, config = elastic_lab("catalog-es")
        self.addCleanup(lab.close)
        es = start_es(lab)
        live = [True]
        def authority():
            now = time.time()
            return {"version": "catalog-revision-authority/1", "generation": "catalog-lab", "epoch": 1,
                    "revision": 2, "deletions": {"v1": 2}, "issued_at": now, "expires_at": now + 30}
        release = CatalogRelease(es, lab.folder / "releases", lambda: live[0], schema=schema,
                                 authority=authority, generation="catalog-lab", epoch=1)
        # A real Bulk HTTP 200 with one mapping failure must remain private.
        request = es.request
        fail_once = [True]

        def partial(method, path, body=None, **kwargs):
            if path == "/_bulk" and fail_once[0]:
                fail_once[0] = False
                lines = body.splitlines()
                broken = json.loads(lines[-1])
                broken["revision"] = "invalid-long"
                lines[-1] = json.dumps(broken).encode()
                body = b"\n".join(lines) + b"\n"
            return request(method, path, body, **kwargs)

        es.request = partial
        with self.assertRaises(UnsafeTransition):
            release.prepare(manifest, documents)
        self.assertIsNone(release.alias_target())
        receipt = release.prepare(manifest, documents)
        self.assertEqual(receipt["count"], 2)
        self.assertEqual(release.prepare(manifest, documents)["count"], 2)
        live[0] = False
        with self.assertRaises(UnsafeTransition):
            release.publish("fixture-r2", expected_current=None)
        self.assertIsNone(release.alias_target())
        live[0] = True
        self.assertEqual(
            release.publish("fixture-r2", expected_current=None)["state"], "PUBLISHED"
        )
        newer = {**manifest, "release_id": "fixture-r3"}
        release.prepare(newer, documents)
        release.publish("fixture-r3", expected_current="venues-fixture-r2")
        release.publish("fixture-r2", expected_current="venues-fixture-r3")
        self.assertEqual(release.alias_target(), "venues-fixture-r2")
        self.assertEqual(
            release.publish("fixture-r2", expected_current="venues-fixture-r3")[
                "state"
            ],
            "PUBLISHED",
        )
        self.assertEqual(request("GET", "/venues-current/_count")["count"], 2)
        (lab.folder / "acceptance.json").write_text(
            json.dumps(
                {
                    "spark_delta_restart": "pass",
                    "duplicate_late_tombstone": "pass",
                    "bulk_partial_retry": "pass",
                    "lease_reject": "pass",
                    "alias_rollback": "pass",
                    "real_s3_app": "not_run",
                    "ready_for_aws": False,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    unittest.main()
