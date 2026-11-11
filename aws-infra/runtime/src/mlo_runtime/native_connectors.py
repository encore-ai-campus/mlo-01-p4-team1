"""Offline, fail-closed Spark connector closure for native hosts and Control drivers.

The 286 Spark distribution JARs and separately supplied connector JARs are
checked against one pinned lock. No Maven, --packages, pip, or AWS operation
is ever invoked here. A bundle is content-addressed and never overwritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .native_releases import file_digest, need

REQUIRED_COORDINATES = {
    "org.apache.spark:spark-sql-kafka-0-10_2.13:4.1.3",
    "org.apache.hadoop:hadoop-aws:3.4.2",
    "io.delta:delta-spark_2.13:4.3.1",
    "org.postgresql:postgresql:42.7.14",
}
HEX256 = re.compile(r"[a-f0-9]{64}\Z")
HEX1 = re.compile(r"[a-f0-9]{40}\Z")
JAR_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.jar\Z")


def locked(path):
    path = Path(path)
    need(path.is_file() and not path.is_symlink(), "native connector lock missing/linked")
    value = json.loads(path.read_text())
    need(value.get("schema_version") == 2 and value.get("spark_version") == "4.1.3"
         and value.get("hadoop_version") == "3.4.2" and value.get("scala_version") == "2.13",
         "native connector lock version mismatch")
    need(set(value.get("coordinates", [])) == REQUIRED_COORDINATES, "connector root coordinates mismatch")
    distribution = value.get("distribution_jars")
    extras = value.get("jars")
    need(isinstance(distribution, list) and len(distribution) == 286, "Spark distribution JAR closure incomplete")
    need(isinstance(extras, list) and len(extras) == 15, "connector JAR closure incomplete")
    provided = value.get("provided_overrides")
    need(isinstance(provided, dict) and "lz4-java" in provided, "distribution lz4 resolution missing")
    seen = set()
    for entry in distribution:
        name, digest = entry.get("filename"), entry.get("sha256")
        need(isinstance(name, str) and JAR_NAME.fullmatch(name)
             and isinstance(digest, str) and HEX256.fullmatch(digest), "invalid Spark JAR pin")
        need(set(entry) == {"filename", "sha256"} and name not in seen, "duplicate/unknown Spark JAR")
        seen.add(name)
    for entry in extras:
        name, digest, coordinate, url = (entry.get(k) for k in ("filename", "sha256", "coordinate", "source"))
        need(isinstance(name, str) and JAR_NAME.fullmatch(name)
             and isinstance(digest, str) and HEX256.fullmatch(digest)
             and isinstance(coordinate, str) and len(coordinate.split(":")) == 3,
             "invalid extra connector pin")
        group, artifact, version = coordinate.split(":")
        need(bool(re.fullmatch(r"[A-Za-z0-9_.-]+", group))
             and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", artifact))
             and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", version))
             and name == artifact + "-" + version + ".jar", "connector filename/coordinate mismatch")
        expected_url = ("https://repo.maven.apache.org/maven2/" + group.replace(".", "/")
                        + "/" + artifact + "/" + version + "/" + name)
        need(url == expected_url, "connector source URL mismatch")
        provenance = entry.get("publisher_sha1")
        need(provenance is None or (isinstance(provenance, str) and HEX1.fullmatch(provenance)),
             "connector provenance checksum format")
        need(name not in seen, "duplicate distribution/extra JAR")
        seen.add(name)
    return value


def lock_sha256(path):
    return file_digest(path)


def release_id(spark_publisher_digest, connector_lock_sha256):
    need(bool(re.fullmatch(r"[a-f0-9]{128}", spark_publisher_digest)), "Spark publisher digest")
    need(bool(HEX256.fullmatch(connector_lock_sha256)), "connector lock digest")
    material = "mlo-spark-runtime-v1\0" + spark_publisher_digest + "\0" + connector_lock_sha256
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def bundle_path(prefix, release_identifier):
    need(bool(re.fullmatch(r"[a-f0-9]{24}", release_identifier)), "connector release ID")
    return Path(prefix) / "connectors" / ("spark-" + release_identifier)


def classpath(prefix, release_identifier):
    return str(bundle_path(prefix, release_identifier) / "jars" / "*")


def verify_jars(directory, entries, *, strict_files=True):
    directory = Path(directory)
    need(directory.is_dir() and not directory.is_symlink(), "JAR directory missing/linked")
    expected = {e["filename"]: e["sha256"] for e in entries}
    actual = {}
    for p in directory.iterdir():
        need(not p.is_symlink(), "JAR directory contains linked path")
        if p.suffix == ".jar":
            need(p.is_file(), "unexpected non-file JAR entry")
            actual[p.name] = p
        elif strict_files:
            need(p.is_file() and p.name.endswith(".jar.sha1")
                 and p.name[:-5] in expected, "unreviewed connector cache entry")
    need(set(actual) == set(expected), "JAR closure: missing/unreviewed/duplicate filenames")
    for name, expected_hash in expected.items():
        need(file_digest(actual[name]) == expected_hash, "JAR checksum mismatch: " + name)
    return {"files": len(expected), "sha256": hashlib.sha256(
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def verify_distribution(spark_home, lock_value):
    return verify_jars(Path(spark_home) / "jars", lock_value["distribution_jars"], strict_files=False)


def verify_extra_cache(directory, lock_value):
    return verify_jars(directory, lock_value["jars"])


def verify_bundle(spark_home, prefix, lock_path, spark_publisher_digest, *, expected_id=None):
    value = locked(lock_path)
    digest = lock_sha256(lock_path)
    identifier = release_id(spark_publisher_digest, digest)
    need(expected_id is None or expected_id == identifier, "Spark+connector release mismatch")
    directory = bundle_path(prefix, identifier)
    need(directory.is_dir() and not directory.is_symlink(), "native connector bundle missing")
    marker = directory / ".mlo-connectors-receipt.json"
    need(marker.is_file() and not marker.is_symlink(), "native connector receipt missing")
    receipt = json.loads(marker.read_text())
    need(receipt == {"schema_version": 1, "spark_publisher_digest": spark_publisher_digest,
                     "connector_lock_sha256": digest, "release_id": identifier,
                     "extra_jars": len(value["jars"])}, "native connector receipt mismatch")
    verify_distribution(spark_home, value)
    verify_jars(directory / "jars", value["jars"])
    need(set(p.name for p in directory.iterdir()) == {"jars", ".mlo-connectors-receipt.json"},
         "unreviewed native bundle entry")
    return {"spark_runtime_release_id": identifier, "connector_lock_sha256": digest,
            "connector_classpath": classpath(prefix, identifier), "extra_jars": len(value["jars"]),
            "distribution_jars": len(value["distribution_jars"])}


def stage_bundle(spark_home, cache, prefix, lock_path, spark_publisher_digest):
    """Stage only; never starts a service or downloads an unpinned dependency."""
    value = locked(lock_path)
    verify_distribution(spark_home, value)
    identifier = release_id(spark_publisher_digest, lock_sha256(lock_path))
    target = bundle_path(prefix, identifier)
    if target.exists() or target.is_symlink():
        return verify_bundle(spark_home, prefix, lock_path, spark_publisher_digest, expected_id=identifier)
    verify_extra_cache(cache, value)
    target.parent.mkdir(parents=True, exist_ok=True)
    need(not target.parent.is_symlink(), "native connector parent is linked")
    temp = Path(tempfile.mkdtemp(prefix=".native-connectors-", dir=target.parent))
    try:
        temp.chmod(0o755)
        jars = temp / "jars"
        jars.mkdir(mode=0o755)
        for entry in value["jars"]:
            dst = jars / entry["filename"]
            with (Path(cache) / entry["filename"]).open("rb") as src, dst.open("xb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            need(file_digest(dst) == entry["sha256"], "staged connector checksum mismatch")
            dst.chmod(0o644)
        receipt = {"schema_version": 1, "spark_publisher_digest": spark_publisher_digest,
                   "connector_lock_sha256": lock_sha256(lock_path),
                   "release_id": identifier, "extra_jars": len(value["jars"])}
        marker = temp / ".mlo-connectors-receipt.json"
        with marker.open("x") as fp:
            json.dump(receipt, fp, sort_keys=True)
            fp.write("\n")
        marker.chmod(0o644)
        os.rename(temp, target)
        temp = None
        return verify_bundle(spark_home, prefix, lock_path, spark_publisher_digest, expected_id=identifier)
    finally:
        if temp is not None:
            shutil.rmtree(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["verify", "stage"])
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--spark-home", type=Path, required=True)
    parser.add_argument("--cache", type=Path, help="pre-staged extra JARs (stage only)")
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--spark-digest", required=True)
    args = parser.parse_args()
    if args.operation == "stage":
        need(args.cache is not None, "connector stage requires reviewed offline cache")
        result = stage_bundle(args.spark_home, args.cache, args.prefix, args.lock, args.spark_digest)
    else:
        result = verify_bundle(args.spark_home, args.prefix, args.lock, args.spark_digest)
    result["ready_for_aws"] = False
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
