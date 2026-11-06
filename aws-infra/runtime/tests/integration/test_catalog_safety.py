"""Independent regressions for deletion rollback and real search field indexing."""
import copy
import time
import unittest

from mlo_runtime.catalog import CatalogRelease, digest
from mlo_runtime.operations import UnsafeTransition
from track2_lab import elastic_lab, start_es


class CatalogSafety(unittest.TestCase):
    def test_current_deletions_survive_rollback_and_old_revisions_are_not_revived(self):
        lab, _ = elastic_lab("catalog-safety")
        self.addCleanup(lab.close)
        es = start_es(lab)
        schema = {"version": "catalog-search/1", "properties": {
            "category": {"type": "keyword"}, "budget": {"type": "long"}, "location": {"type": "geo_point"}}}
        current = {"revision": 1, "deletions": {}}
        live = [True]

        def authority():
            now = time.time()
            return {"version": "catalog-revision-authority/1", "generation": "safety-lab", "epoch": 8,
                    **copy.deepcopy(current), "issued_at": now, "expires_at": now + 30}

        publisher = CatalogRelease(es, lab.folder / "releases", lambda: live[0], schema=schema,
                                   authority=authority, generation="safety-lab", epoch=8)

        def prepare(name, rows):
            manifest = {"version": "0.2.0", "query": "venues-catalog", "release_id": name,
                        "dataset_version": "fixture-v1", "complete": True, "schema_sha256": digest(schema),
                        "files": {"fixture.ndjson": "a" * 64}, "records": len(rows), "documents_sha256": digest(rows)}
            return publisher.prepare(manifest, rows)

        rows = [{"id": "venue-a", "revision": 1, "deleted": False,
                 "document": {"category": "CAFE", "budget": 15000, "location": {"lat": 37.6, "lon": 126.9}}}]
        prepare("safety-r1", rows)
        publisher.publish("safety-r1", expected_current=None)
        for query in ({"term": {"document.category": "CAFE"}},
                      {"range": {"document.budget": {"lte": 20000}}},
                      {"geo_distance": {"distance": "1km", "document.location": {"lat": 37.6, "lon": 126.9}}}):
            self.assertEqual(es.request("POST", "/venues-current/_search", {"query": query})["hits"]["total"]["value"], 1)
        current.update(revision=2, deletions={"venue-a": 2})
        prepare("safety-r2", [{"id": "venue-a", "revision": 2, "deleted": True, "document": {}}])
        publisher.publish("safety-r2", expected_current="venues-safety-r1")
        result = publisher.publish("safety-r1", expected_current="venues-safety-r2")
        self.assertNotEqual(result["index"], "venues-safety-r1")
        self.assertEqual(result["count"], 0)
        self.assertEqual(es.request("GET", "/venues-current/_count")["count"], 0)
        self.assertFalse(es.request("POST", "/venues-current/_mget", {"ids": ["venue-a"]})["docs"][0]["found"])
        prepare("safety-delayed", rows)
        publisher.publish("safety-delayed", expected_current=result["index"])
        self.assertEqual(es.request("GET", "/venues-current/_count")["count"], 0)
        unchanged = publisher.alias_target()
        current.update(revision=1, deletions={})
        with self.assertRaises(UnsafeTransition):
            publisher.publish("safety-r1", expected_current=unchanged)
        self.assertEqual(publisher.alias_target(), unchanged)
        current.update(revision=2, deletions={"venue-a": 2})
        publisher.authority = None
        with self.assertRaises(UnsafeTransition):
            publisher.publish("safety-r1", expected_current=unchanged)
        self.assertEqual(publisher.alias_target(), unchanged)
        publisher.authority = authority
        live[0] = False
        with self.assertRaises(UnsafeTransition):
            publisher.publish("safety-r1", expected_current=unchanged)
        self.assertEqual(publisher.alias_target(), unchanged)


if __name__ == "__main__":
    unittest.main()
