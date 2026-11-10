"""Stage a reviewed app snapshot in a disposable Control-host lab root.

This intentionally cannot install onto a production host. Real Control host
acceptance requires host identity, mounts, Python/PKI closure, and a separately
approved immutable remote artifact. Never flips readiness or starts a writer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import zipfile

from .native_releases import need
from .native_app import FEATURE_SCOPE
from .contracts import ROOT

HEX256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def sha256(path):
    with Path(path).open("rb") as fp:
        return hashlib.file_digest(fp, "sha256").hexdigest()


def inspect_source(source):
    folder = Path(source)
    need(folder.is_dir() and not folder.is_symlink(), "reviewed shared app directory required")
    manifest = folder / "manifest.json"
    need(manifest.is_file() and not manifest.is_symlink(), "reviewed app manifest missing")
    value = json.loads(manifest.read_text())
    python_wheel_lock = ROOT / "aws-infra/runtime/native-feedback-python.lock.json"
    need(python_wheel_lock.is_file() and not python_wheel_lock.is_symlink(),
         "reviewed feedback wheel lock missing")
    wheel_meta = json.loads(python_wheel_lock.read_text())
    need(value.get("native_feedback_python_wheel_lock_sha256") == sha256(python_wheel_lock)
         and wheel_meta.get("source_requirements_sha256") == value.get("python_requirements_lock_sha256"),
         "Control app and locked Python release identity changed")
    need(value.get("schema_version") == "native-shared-app-review-v3"
         and value.get("source_repository") == "encore-ai-campus/mlo-01-p4-team1"
         and value.get("feedback_backend_sources_bundled") is True
         and value.get("feedback_requires_offline_python_closure") is True
         and value.get("runtime_feature_scope") == FEATURE_SCOPE
         and value.get("python_requirements_lock_sha256") == value.get("source_files", {}).get("backend/requirements.lock")
         and value.get("accepted_for_control") is False
         and value.get("ready_for_aws") is False
         and value.get("legacy_submit_wrapper_excluded") is True
         and bool(COMMIT.fullmatch(value.get("pinned_develop_commit", ""))),
         "candidate app snapshot policy mismatch")
    archive = folder / "pipeline-reviewed.zip"
    need(archive.is_file() and not archive.is_symlink()
         and sha256(archive) == value["pipeline_zip_sha256"], "reviewed pipeline ZIP checksum mismatch")
    with zipfile.ZipFile(archive) as data:
        names = data.namelist()
        need(len(names) == len(set(names))
             and len(names) == value["source_file_count"]
             and set(names) == set(value["source_files"]), "reviewed app ZIP file closure mismatch")
        need(not any(name.endswith("submit_spark.py") for name in names), "incompatible wrapper in ZIP")
        for name in names:
            need(name.startswith(("pipeline/", "backend/")) and ".." not in Path(name).parts,
                 "reviewed ZIP path outside pinned application")
            body = data.read(name)
            need(hashlib.sha256(body).hexdigest() == value["source_files"][name],
                 "reviewed ZIP source entry checksum mismatch")
    entries = {"pipeline-reviewed.zip": value["pipeline_zip_sha256"]}
    for query, spec in value["entrypoints"].items():
        need(query in {"weather-stream", "feedback-postgresql"}
             and set(spec) == {"filename", "sha256"},
             "reviewed driver entrypoint not supported")
        name = spec["filename"]
        need(name == query + "-spark_job.py" and bool(HEX256.fullmatch(spec["sha256"])),
             "reviewed driver name/hash mismatch")
        p = folder / name
        need(p.is_file() and not p.is_symlink()
             and sha256(p) == spec["sha256"], "reviewed driver entrypoint changed")
        entries[name] = spec["sha256"]
    need(set(entries) == {"pipeline-reviewed.zip", "weather-stream-spark_job.py",
                          "feedback-postgresql-spark_job.py"}, "reviewed application closure incomplete")
    return value, entries


def target_path(lab_root, manifest):
    root = Path(lab_root).absolute()
    need(root.exists() and root.is_dir() and not root.is_symlink()
         and root != Path("/") and root.name.startswith("mlo-control-lab-"),
         "disposable Control lab root required (production forbidden)")
    for parent in root.parents:
        need(not parent.is_symlink(), "Control lab ancestor symlink forbidden")
    leaf = ("source-" + manifest["pinned_develop_commit"][:12] + "-"
            + manifest["pipeline_zip_sha256"][:12])
    dest = root / "opt/mlo/current/apps" / leaf
    for path in [dest, *dest.parents]:
        if path == root.parent:
            break
        need(not path.is_symlink(), "Control lab target symlink forbidden")
    return dest


def verify(source, lab_root):
    manifest, entries = inspect_source(source)
    target = target_path(lab_root, manifest)
    need(target.is_dir() and not target.is_symlink(), "Control app stage missing")
    need(set(p.name for p in target.iterdir()) == set(entries) | {".mlo-app-review-receipt.json"},
         "unreviewed staged Control app file")
    for name, digest in entries.items():
        p = target / name
        need(p.is_file() and not p.is_symlink() and sha256(p) == digest,
             "Control staged application checksum mismatch")
    receipt_path = target / ".mlo-app-review-receipt.json"
    need(receipt_path.is_file() and not receipt_path.is_symlink(), "Control app receipt missing")
    receipt = json.loads(receipt_path.read_text())
    need(receipt == {"schema_version": 1,
                     "pinned_develop_commit": manifest["pinned_develop_commit"],
                     "files": entries,
                     "accepted": False}, "Control staging receipt mismatch")
    return {"lab_only": True, "ready_for_aws": False,
            "source_commit": manifest["pinned_develop_commit"],
            "files": entries, "root": str(lab_root),
            "weather_entrypoint": str(target / "weather-stream-spark_job.py"),
            "feedback_entrypoint": str(target / "feedback-postgresql-spark_job.py"),
            "pipeline_zip": str(target / "pipeline-reviewed.zip")}


def stage(source, lab_root):
    manifest, entries = inspect_source(source)
    target = target_path(lab_root, manifest)
    if target.exists() or target.is_symlink():
        return verify(source, lab_root)
    parent = target.parent
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    need(not parent.is_symlink(), "Control staging parent symlink forbidden")
    tmp = Path(tempfile.mkdtemp(prefix=".mlo-control-candidate-", dir=parent))
    try:
        tmp.chmod(0o755)
        for name, expected in entries.items():
            output = tmp / name
            with (Path(source) / name).open("rb") as reader, output.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            output.chmod(0o644)
            need(sha256(output) == expected, "Control staging copy changed")
        marker = tmp / ".mlo-app-review-receipt.json"
        with marker.open("x") as writer:
            json.dump({"schema_version": 1,
                       "pinned_develop_commit": manifest["pinned_develop_commit"],
                       "files": entries, "accepted": False}, writer, sort_keys=True)
            writer.write("\n")
        marker.chmod(0o644)
        os.rename(tmp, target)
        tmp = None
        return verify(source, lab_root)
    finally:
        if tmp is not None:
            shutil.rmtree(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["stage", "verify"])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--lab-root", type=Path, required=True)
    arguments = parser.parse_args()
    result = stage(arguments.source, arguments.lab_root) if arguments.operation == "stage" else (
        verify(arguments.source, arguments.lab_root))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
