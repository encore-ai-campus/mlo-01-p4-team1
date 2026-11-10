"""Root-owned systemd ExecStartPre guard for the exact native Spark JAR closure.

Standalone stdlib-only script: run by systemd as root before each Spark daemon
restart, not by an application user. Never downloads or changes any files.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_files(directory, items):
    p = Path(directory)
    need(p.is_dir() and not p.is_symlink(), "native Spark JAR directory missing")
    expected = {i["filename"]: i["sha256"] for i in items}
    need(len(expected) == len(items), "native Spark duplicate JAR pin")
    actual = {}
    for entry in p.iterdir():
        need(not entry.is_symlink(), "native Spark linked JAR entry")
        if entry.suffix == ".jar":
            need(entry.is_file(), "native Spark unexpected JAR type")
            actual[entry.name] = entry
    need(set(actual) == set(expected), "native Spark unreviewed/missing JAR")
    for name, sha256 in expected.items():
        need(digest(actual[name]) == sha256, "native Spark altered JAR")


def check(binding_name, connectors_name, releases_name, spark_home, bundle, expected_id):
    binding_file = Path(binding_name)
    need(binding_file == Path("/etc/mlo/host-binding.json") and not binding_file.is_symlink(),
         "native guard binding path")
    s = binding_file.stat()
    need(s.st_uid == 0 and stat.S_IMODE(s.st_mode) & 0o077 == 0, "native guard binding protection")
    binding = json.loads(binding_file.read_text())
    need(binding.get("lab") is not True and binding.get("root", "/") == "/"
         and binding.get("role") == "spark" and binding.get("execution_target") == "native-systemd-v1",
         "native guard host identity")
    lock_file, release_file = Path(connectors_name), Path(releases_name)
    need(all(p.is_file() and not p.is_symlink() and p.stat().st_uid == 0
             and stat.S_IMODE(p.stat().st_mode) & 0o022 == 0
             for p in (lock_file, release_file)), "native guard locks not root-protected")
    lock_sha = digest(lock_file)
    need(lock_sha == binding.get("native_connector_lock_sha256")
         and digest(release_file) == binding.get("native_release_lock_sha256"),
         "native guard accepted lock mismatch")
    value = json.loads(lock_file.read_text())
    releases = json.loads(release_file.read_text())
    publisher = releases["releases"]["spark"]["digest"]
    need(value.get("schema_version") == 2 and value.get("spark_version") == "4.1.3"
         and len(value.get("distribution_jars", [])) == 286
         and len(value.get("jars", [])) == 15, "native guard connector closure")
    need(bool(re.fullmatch(r"[a-f0-9]{128}", publisher)), "native guard publisher digest")
    calculated = hashlib.sha256(("mlo-spark-runtime-v1\0" + publisher + "\0" + lock_sha).encode()).hexdigest()[:24]
    need(expected_id == calculated, "native guard effective Spark release mismatch")
    need(Path(spark_home) == Path("/opt/mlo/native/releases/spark-" + publisher[:16])
         and Path(bundle) == Path("/opt/mlo/native/connectors/spark-" + calculated),
         "native guard immutable paths changed")
    extra_dir = Path(bundle) / "jars"
    need(not Path(bundle).is_symlink(), "native guard connector bundle linked")
    receipt = Path(bundle) / ".mlo-connectors-receipt.json"
    need(receipt.is_file() and not receipt.is_symlink(), "native guard connector receipt")
    metadata = json.loads(receipt.read_text())
    need(metadata == {"schema_version": 1, "spark_publisher_digest": publisher,
                      "connector_lock_sha256": lock_sha, "release_id": calculated,
                      "extra_jars": 15}, "native guard connector receipt mismatch")
    need(set(p.name for p in Path(bundle).iterdir()) == {"jars", ".mlo-connectors-receipt.json"},
         "native guard unreviewed connector bundle entry")
    expected_dist = {x["filename"] for x in value["distribution_jars"]}
    expected_extra = {x["filename"] for x in value["jars"]}
    need(not expected_dist & expected_extra, "native guard duplicated classpath artifact")
    verify_files(Path(spark_home) / "jars", value["distribution_jars"])
    verify_files(extra_dir, value["jars"])


if __name__ == "__main__":
    need(len(sys.argv) == 7, "native guard expected six bound arguments")
    check(*sys.argv[1:])
