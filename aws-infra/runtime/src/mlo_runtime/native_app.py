"""Deterministic review snapshot of the *shared* develop application at a pinned commit.

Read-only Git object access: never checks out, fetches, merges or changes source.
Produces an offline Python app archive and driver entrypoints, not a deployable
or an accepted host release. Nothing here starts Spark or accesses AWS.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

from .native_releases import need
from .contracts import ROOT

COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
PIPELINE_PREFIX = "app/pipeline/"
SOURCE_PREFIXES = ("app/pipeline/", "app/backend/")
ALLOWED_SUFFIXES = {".py", ".json", ".sql", ".lock"}
ENTRYPOINTS = {
    "weather-stream": "app/pipeline/weather/spark_job.py",
    "feedback-postgresql": "app/pipeline/feedback/spark_job.py",
}
FORBIDDEN = (".env", ".pem", ".key", ".p12", ".sqlite3", "credentials", ".private")
FEATURE_SCOPE = {
    "required": ["spark-kafka-structured-streaming", "s3a-checkpoints", "postgresql-feedback", "weather-elasticsearch-http"],
    "excluded_unverified": ["unity-catalog-client", "unity-catalog-hadoop", "delta-kernel-unity-catalog"],
    "delta_local_filesystem": "independent-smoke-only-not-used-by-current-app",
}
OPTIONAL_FEATURE_PATTERN = re.compile(
    r"unitycatalog|io\.delta\.|\bDeltaTable\b|\.format\(\s*['\"]delta['\"]|"
    r"spark\.sql\.extensions|\bfrom\s+delta\b|\bimport\s+delta\b", re.I)



def _git(repository, *argv):
    result = subprocess.run(["git", "-C", str(repository), *argv],
                            capture_output=True, check=False, timeout=45)
    need(result.returncode == 0, "pinned shared app Git object is unavailable")
    return result.stdout


def _entries(repository, commit):
    raw = _git(repository, "ls-tree", "-r", "-z", commit, "--", "app/pipeline", "app/backend")
    selected = {}
    for item in raw.split(b"\x00"):
        if not item:
            continue
        header, name_raw = item.split(b"\t", 1)
        mode, obj_type, oid = header.decode().split(" ", 2)
        path = name_raw.decode("utf-8")
        need(path.startswith(SOURCE_PREFIXES), "app Git path outside accepted source roots")
        short = PurePosixPath(path[len("app/"):])
        need(".." not in short.parts and not short.is_absolute(), "unsafe pipeline path")
        if short.suffix not in ALLOWED_SUFFIXES:
            continue
        need(mode in {"100644", "100755"} and obj_type == "blob", "unreviewed Git executable/link/submodule")
        need(not any(x in path.lower() for x in FORBIDDEN), "private material in application selection")
        if path == "app/pipeline/weather/submit_spark.py":
            continue  # mutually incompatible, do not package old inline-secret wrapper
        selected[str(short)] = oid
    need(all("pipeline/" + p.split(PIPELINE_PREFIX, 1)[1] in selected
             for p in ENTRYPOINTS.values()), "shared application Spark job entrypoint missing")
    need(all(name in selected for name in ("pipeline/weather/mapping.json",
                                           "backend/cafe/settings.py", "backend/common/events.py",
                                           "backend/common/models.py", "backend/requirements.lock")),
         "shared feedback Django/requirements source closure incomplete")
    need(len(selected) >= 200, "shared application+backend source closure incomplete")
    return selected


def prepare(repository, commit, output):
    repo = Path(repository).resolve()
    need(repo.is_dir() and (repo / ".git").exists(), "shared repo must be a Git worktree")
    need(bool(COMMIT_RE.fullmatch(commit)), "full pinned shared develop SHA required")
    need(_git(repo, "cat-file", "-t", commit).strip() == b"commit", "shared develop source object missing")
    output = Path(output)
    need(not output.exists() and not output.is_symlink(), "new application review folder required")
    entries = _entries(repo, commit)
    # Read and hash *all* selected blob contents before publishing any output.
    source = {}
    for relative, oid in sorted(entries.items()):
        payload = _git(repo, "cat-file", "blob", oid)
        need(len(payload) <= 8_000_000, "unexpected oversized application blob")
        source[relative] = payload
    optional_feature_paths = [name for name, content in source.items()
                              if name.endswith(".py") and OPTIONAL_FEATURE_PATTERN.search(content.decode("utf-8"))]
    need(not optional_feature_paths, "unreviewed Unity Catalog/Delta application usage requires separate closure")
    python_wheel_lock = ROOT / "aws-infra/runtime/native-feedback-python.lock.json"
    need(python_wheel_lock.is_file() and not python_wheel_lock.is_symlink(),
         "native Python wheel release lock missing")
    wheel_meta = json.loads(python_wheel_lock.read_text())
    need(wheel_meta.get("schema_version") == "native-feedback-python-lock-v1"
         and wheel_meta.get("python") == "CPython-3.12"
         and wheel_meta.get("source_requirements_sha256") ==
         hashlib.sha256(source["backend/requirements.lock"]).hexdigest(),
         "latest shared app and Native Python wheel lock disagree")
    output.mkdir(parents=True, mode=0o700)
    manifest = {
        "schema_version": "native-shared-app-review-v3",
        "source_repository": "encore-ai-campus/mlo-01-p4-team1",
        "pinned_develop_commit": commit,
        "source_files": {k: hashlib.sha256(v).hexdigest() for k, v in source.items()},
        "source_file_count": len(source),
        "ready_for_aws": False,
        "accepted_for_control": False,
        "approval_required": True,
        "legacy_submit_wrapper_excluded": True,
        "feedback_backend_sources_bundled": True,
        "runtime_feature_scope": FEATURE_SCOPE,
        "feedback_requires_offline_python_closure": True,
        "python_requirements_lock_sha256": hashlib.sha256(source["backend/requirements.lock"]).hexdigest(),
        "native_feedback_python_wheel_lock_sha256": hashlib.sha256(python_wheel_lock.read_bytes()).hexdigest(),
    }
    archive = output / "pipeline-reviewed.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=9) as stream:
        for name, body in sorted(source.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 << 16)
            stream.writestr(info, body)
    manifest["pipeline_zip_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest["entrypoints"] = {}
    for query, path in ENTRYPOINTS.items():
        name = query + "-spark_job.py"
        body = source["pipeline/" + path[len(PIPELINE_PREFIX):]]
        (output / name).write_bytes(body)
        manifest["entrypoints"][query] = {
            "filename": name, "sha256": hashlib.sha256(body).hexdigest(),
        }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.repository, args.commit, args.output)
    print(json.dumps({"files": result["source_file_count"],
                      "archive_sha256": result["pipeline_zip_sha256"],
                      "source_commit": result["pinned_develop_commit"],
                      "accepted_for_control": False, "ready_for_aws": False}))


if __name__ == "__main__":
    main()
