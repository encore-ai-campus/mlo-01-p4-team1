"""Execute a complete accepted export into a private versioned ES index.

The Spark/application artifact owns transformations and revisions. This module
checks the handoff, writes idempotently, verifies every result, and publishes.
No PG overlay, permissions, Outbox or user-facing search API is implemented here.
"""

import base64
import hashlib
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .operations import UnsafeTransition, atomic_json, local_lock
from .track2 import identity, require, sha256


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def accept_export(manifest, folder, *, expected_schema):
    require(
        set(manifest)
        == {
            "version",
            "query",
            "release_id",
            "dataset_version",
            "complete",
            "schema_sha256",
            "files",
            "records",
            "documents_sha256",
        },
        "unknown release manifest",
    )
    require(
        manifest["version"] == "0.2.0"
        and manifest["query"] == "venues-catalog"
        and manifest["complete"] is True,
        "incomplete/unaccepted catalog export",
    )
    identity(manifest["release_id"])
    identity(manifest["dataset_version"])
    sha256(expected_schema)
    require(manifest["schema_sha256"] == expected_schema, "catalog schema mismatch")
    require(
        type(manifest["records"]) is int and 0 <= manifest["records"] <= 1000000,
        "bounded manifest count required",
    )
    require(
        isinstance(manifest["files"], dict) and 0 < len(manifest["files"]) <= 1024,
        "bounded immutable file manifest required",
    )
    folder = Path(folder).resolve()
    documents = {}
    size = 0
    for name, checksum in sorted(manifest["files"].items()):
        path = folder / name
        require(
            not Path(name).is_absolute()
            and ".." not in Path(name).parts
            and not path.is_symlink()
            and path.resolve().is_relative_to(folder),
            "export path escape",
        )
        require(
            path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
            "export file missing/oversized",
        )
        data = path.read_bytes()
        size += len(data)
        require(
            size <= 256 * 1024 * 1024,
            "local export memory bound exceeded; partition artifact required",
        )
        require(
            hashlib.sha256(data).hexdigest() == sha256(checksum),
            "immutable export checksum mismatch",
        )
        for line in data.splitlines():
            require(len(line) <= 65536, "export document oversized")
            item = json.loads(line)
            require(
                set(item) == {"id", "revision", "deleted", "document"},
                "invalid export record",
            )
            identity(item["id"])
            require(
                type(item["revision"]) is int
                and 0 < item["revision"] < 2**63
                and type(item["deleted"]) is bool
                and isinstance(item["document"], dict),
                "invalid revision/tombstone",
            )
            require(
                not item["deleted"] or item["document"] == {},
                "tombstone must not retain payload",
            )
            previous = documents.get(item["id"])
            if previous and previous["revision"] == item["revision"]:
                require(previous == item, "conflicting duplicate revision")
            if previous is None or previous["revision"] < item["revision"]:
                documents[item["id"]] = item
    selected = [documents[k] for k in sorted(documents)]
    require(
        len(selected) == manifest["records"]
        and digest(selected) == manifest["documents_sha256"],
        "release manifest completeness/hash mismatch",
    )
    return selected


class Elastic:
    """Small bounded REST boundary; credentials and response bodies are not logged."""

    def __init__(self, url, ca, username, password):
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.path in {"", "/"}
            and not parsed.username
            and not parsed.query
            and not parsed.fragment,
            "verified ES HTTPS origin required",
        )
        self.url = url.rstrip("/")
        self.auth = (
            "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        )
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca)),
        )

    def request(self, method, path, body=None, *, ndjson=False, missing=False):
        data = body if ndjson else canonical(body) if body is not None else None
        headers = {
            "Authorization": self.auth,
            "Content-Type": "application/x-ndjson" if ndjson else "application/json",
        }
        request = urllib.request.Request(
            self.url + path, data=data, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                require(len(data) <= 32 * 1024 * 1024, "ES response bound exceeded")
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            if missing and exc.code == 404:
                return None
            raise UnsafeTransition(
                "ES request rejected: HTTP " + str(exc.code)
            ) from None


class CatalogRelease:
    """Publish immutable snapshots only with an accepted search schema and live deletion authority.

    ``authority`` is a protected application/runtime adapter, like ``guard``;
    a browser-supplied JSON document is never an authority implementation.
    The same callback is required on failover and rollback. Local state records
    observed high-water marks but never substitutes for the current source.
    """

    def __init__(self, es, state, guard, *, alias="venues-current", schema=None,
                 authority=None, generation=None, epoch=None):
        self.es, self.state, self.guard, self.alias = es, Path(state), guard, identity(alias)
        self.schema, self.authority = schema, authority
        self.generation, self.epoch = generation, epoch

    def _guard(self):
        require(self.guard() is True, "catalog admission lost lease/fencing")

    def _schema(self, expected):
        require(isinstance(self.schema, dict) and set(self.schema) == {"version", "properties"}
                and self.schema["version"] == "catalog-search/1", "accepted search schema required")
        props = self.schema["properties"]
        require(isinstance(props, dict) and 0 < len(props) <= 64, "bounded search mapping required")
        import re
        for field, rule in props.items():
            require(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field) and isinstance(rule, dict)
                    and set(rule) == {"type"} and rule["type"] in
                    {"keyword", "text", "long", "double", "geo_point", "boolean", "date"},
                    "unsupported/unsearchable accepted field mapping")
        require(digest(self.schema) == expected, "search mapping/schema digest mismatch")
        return {"type": "object", "dynamic": "strict", "properties": props}

    def alias_target(self):
        result = self.es.request("GET", "/_alias/" + self.alias, missing=True)
        require(result is None or len(result) == 1, "alias must have exactly one validated release")
        return next(iter(result)) if result else None

    def _searchable(self, index, documents):
        """Exercise the supplied keyword/text/range/geo mapping, not just _source GET."""
        live = [d for d in documents if not d["deleted"]]
        caps = self.es.request("POST", "/" + index + "/_field_caps",
                               {"fields": ["document." + f for f in self.schema["properties"]]})["fields"]
        for field, rule in self.schema["properties"].items():
            name, kind = "document." + field, rule["type"]
            require(name in caps and caps[name].get(kind, {}).get("searchable") is True,
                    "accepted field is not indexed/searchable")
            row = next((d for d in live if d["document"].get(field) is not None), None)
            if row is None:
                continue  # Nullable/empty snapshots still have validated searchable mappings.
            value = row["document"][field]
            if kind == "geo_point":
                query = {"geo_distance": {"distance": "1m", name: value}}
            elif kind in {"long", "double", "date"}:
                query = {"range": {name: {"gte": value, "lte": value}}}
            elif kind == "text":
                query = {"match_phrase": {name: value}}
            else:
                query = {"term": {name: value}}
            found = self.es.request("POST", "/" + index + "/_search", {
                "size": 1, "_source": False,
                "query": {"bool": {"filter": [{"ids": {"values": [row["id"]]}}, query]}}})
            require([hit["_id"] for hit in found["hits"]["hits"]] == [row["id"]],
                    "accepted search query did not find indexed content")

    def _materialize(self, index, manifest, documents):
        metadata = {"release_sha256": digest(manifest), "schema_sha256": manifest["schema_sha256"],
                    "documents_sha256": digest(documents)}
        mapping = self._schema(manifest["schema_sha256"])
        old = self.es.request("GET", "/" + index, missing=True)
        if old:
            require(old[index]["mappings"].get("_meta") == metadata, "immutable release index already differs")
        else:
            self._guard()
            self.es.request("PUT", "/" + index, {"settings": {"number_of_shards": 1},
                "mappings": {"_meta": metadata, "dynamic": "strict", "properties": {
                    "revision": {"type": "long"}, "release_id": {"type": "keyword"}, "document": mapping}}})
        sealed = bool(old and old[index].get("settings", {}).get("index", {}).get("blocks", {}).get("write") == "true")
        for start in ([] if sealed else range(0, len(documents), 128)):
            self._guard()
            lines = []
            for item in documents[start:start + 128]:
                if not item["deleted"]:
                    lines += [canonical({"index": {"_index": index, "_id": item["id"],
                              "version": item["revision"], "version_type": "external_gte"}}),
                              canonical({"revision": item["revision"], "release_id": manifest["release_id"],
                                         "document": item["document"]})]
            if lines:
                response = self.es.request("POST", "/_bulk", b"\n".join(lines) + b"\n", ndjson=True)
                require(response.get("errors") is False and len(response.get("items", [])) == len(lines)//2
                        and all(200 <= i.get("index", {}).get("status", 0) < 300 for i in response["items"]),
                        "ES bulk partial failure; private release retained for retry")
        self._guard()
        self.es.request("POST", "/" + index + "/_refresh")
        expected = {d["id"]: {"revision": d["revision"], "release_id": manifest["release_id"],
                              "document": d["document"]} for d in documents if not d["deleted"]}
        require(self.es.request("GET", "/" + index + "/_count")["count"] == len(expected), "release count incomplete")
        for start in range(0, len(expected), 128):
            ids = list(expected)[start:start+128]
            actual = self.es.request("POST", "/" + index + "/_mget", {"ids": ids})["docs"]
            require(len(actual) == len(ids) and {i.get("_id") for i in actual} == set(ids)
                    and all(i.get("found") is True and i["_source"] == expected[i["_id"]] for i in actual),
                    "release searchable content mismatch")
        self._searchable(index, documents)
        self._guard()
        self.es.request("PUT", "/" + index + "/_settings", {"index.blocks.write": True})
        return metadata, len(expected)

    def prepare(self, manifest, documents):
        self._guard()
        release_id = identity(manifest["release_id"])
        require(digest(documents) == manifest["documents_sha256"] and len(documents) == manifest["records"]
                and manifest["complete"] is True, "export changed after acceptance")
        self._schema(manifest["schema_sha256"])
        with local_lock(self.state / "catalog.lock"):
            index = "venues-" + release_id
            snapshot = {"manifest": manifest, "documents": documents, "schema": self.schema}
            snapshot_path = self.state / (release_id + ".snapshot.json")
            if snapshot_path.exists():
                require(json.loads(snapshot_path.read_text()) == snapshot, "immutable accepted snapshot differs")
            metadata, count = self._materialize(index, manifest, documents)
            atomic_json(snapshot_path, snapshot)
            receipt = {"index": index, "base_index": index, **metadata, "snapshot_sha256": digest(snapshot),
                       "state": "PUBLISHED" if self.alias_target() == index else "SEARCHABLE",
                       "count": count, "ready_for_aws": False}
            atomic_json(self.state / (release_id + ".json"), receipt)
            return receipt

    def _authority(self):
        import time
        require(callable(self.authority) and self.generation is not None
                and type(self.epoch) is int and self.epoch > 0, "live deletion authority adapter required")
        proof = self.authority()
        require(isinstance(proof, dict) and set(proof) == {"version", "generation", "epoch", "revision",
                "deletions", "issued_at", "expires_at"}, "invalid deletion authority proof")
        now = time.time()
        require(proof["version"] == "catalog-revision-authority/1" and proof["generation"] == self.generation
                and type(proof["epoch"]) is int and proof["epoch"] == self.epoch
                and type(proof["revision"]) is int and proof["revision"] >= 0,
                "deletion authority generation/epoch/revision mismatch")
        require(type(proof["issued_at"]) in {int, float} and type(proof["expires_at"]) in {int, float}
                and now-60 <= proof["issued_at"] <= now < proof["expires_at"] <= proof["issued_at"]+60,
                "deletion authority stale or unbounded")
        deletions = proof["deletions"]
        require(isinstance(deletions, dict) and len(deletions) <= 1000000, "bounded deletion revisions required")
        for name, revision in deletions.items():
            identity(name)
            require(type(revision) is int and 0 < revision < 2**63, "invalid deletion revision")
        stable = {k: proof[k] for k in ("version", "generation", "epoch", "revision", "deletions")}
        previous = self.state / "authority-observed.json"
        if previous.exists():
            old = json.loads(previous.read_text())
            require(old["generation"] == stable["generation"] and old["epoch"] <= stable["epoch"]
                    and old["revision"] <= stable["revision"]
                    and all(deletions.get(k, 0) >= v for k,v in old["deletions"].items()),
                    "deletion authority regressed; reconcile source before publish")
            if old["revision"] == stable["revision"]:
                require(old["deletions"] == stable["deletions"], "same authority revision changed")
        return stable

    def publish(self, release_id, *, expected_current):
        """Rebuild a rollback variant with current deletions, never revive a stale index."""
        with local_lock(self.state / "catalog.lock"):
            self._guard()
            release_id = identity(release_id)
            receipt = json.loads((self.state / (release_id + ".json")).read_text())
            snapshot = json.loads((self.state / (release_id + ".snapshot.json")).read_text())
            require(receipt["state"] in {"SEARCHABLE", "PUBLISHED"}
                    and digest(snapshot) == receipt["snapshot_sha256"]
                    and digest(snapshot["manifest"]) == receipt["release_sha256"]
                    and snapshot["schema"] == self.schema, "unvalidated or changed release cannot publish")
            manifest, documents = snapshot["manifest"], snapshot["documents"]
            require(manifest["release_id"] == release_id and digest(documents) == manifest["documents_sha256"],
                    "release snapshot binding mismatch")
            authority = self._authority()
            for row in documents:
                if row["deleted"]:
                    require(authority["deletions"].get(row["id"], 0) >= row["revision"],
                            "source deletion authority has not accepted exported tombstone")
            selected = [d for d in documents if d["deleted"] or d["revision"] > authority["deletions"].get(d["id"], 0)]
            index = "venues-" + release_id
            if selected != documents:
                index += "-d" + str(authority["revision"]) + "-" + digest(selected)[:12]
            metadata, count = self._materialize(index, manifest, selected)
            require(self._authority() == authority, "deletion authority changed while building; retry private candidate")
            atomic_json(self.state / "authority-observed.json", authority)
            current = self.alias_target()
            if current != index:
                require(current == expected_current, "alias changed; reconcile before cutover")
                actions = [{"remove": {"index": current, "alias": self.alias, "must_exist": True}}] if current else []
                actions.append({"add": {"index": index, "alias": self.alias, "is_write_index": False}})
                self._guard()
                require(self._authority() == authority, "deletion authority changed before publication")
                result = self.es.request("POST", "/_aliases", {"actions": actions})
                require(result.get("acknowledged") is True and not result.get("errors", False),
                        "alias acknowledgement uncertain")
            require(self.alias_target() == index, "alias publication unconfirmed")
            require(self._authority() == authority, "deletion authority changed; publication requires reconciliation")
            receipt.update(index=index, state="PUBLISHED", count=count, previous=expected_current,
                           authority_sha256=digest(authority), authority_revision=authority["revision"],
                           effective_documents_sha256=metadata["documents_sha256"])
            atomic_json(self.state / (release_id + ".json"), receipt)
            return receipt
