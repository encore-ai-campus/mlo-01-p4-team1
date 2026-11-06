"""Trusted, stdlib-only runtime artifact verification for controller and SSM hosts."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_TAR_BYTES = MAX_TOTAL_BYTES + 8 * 1024 * 1024
MAX_MEMBERS = 4096
OPERATIONS = {"install", "deploy", "health", "rollback"}
ROLES = {"control", "gateway", "postgresql", "mongodb", "kafka", "spark", "elasticsearch", "advertising"}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class ContractError(ValueError):
    pass


def require(value, message):
    if not value:
        raise ContractError(message)


def canonical_path(name):
    require(isinstance(name, str) and name and "\\" not in name and "\x00" not in name, "unsafe artifact path")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ".." not in path.parts and str(path) == name and name != ".", "unsafe artifact path")
    require(all(re.fullmatch(r"[A-Za-z0-9_@.+-]+", part) for part in path.parts), "non-canonical artifact path")
    return name


def unique_json(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate manifest key")
        value[key] = item
    return value


def validate_manifest(manifest, environment, generation, node):
    required = {"schema_version", "artifact_version", "environment", "generation", "platform", "example_only", "target_nodes", "node_roles", "operations", "files", "image_digests", "config_hashes", "required_secrets"}
    require(isinstance(manifest, dict) and required <= set(manifest) and set(manifest) <= required | {"fixture"}, "artifact manifest fields mismatch")
    require(manifest["schema_version"] == "1.0.0", "artifact schema mismatch")
    require(all(isinstance(manifest[k], str) and NAME.fullmatch(manifest[k]) for k in ("artifact_version", "environment", "generation")), "invalid artifact identity")
    require((manifest["environment"], manifest["generation"]) == (environment, generation), "artifact target mismatch")
    require(manifest["example_only"] is False and manifest["platform"] == "linux/amd64", "real amd64 payload required")
    require(isinstance(manifest.get("fixture", False), bool), "invalid fixture marker")
    nodes = manifest["target_nodes"]
    require(isinstance(nodes, list) and 0 < len(nodes) <= 16 and all(isinstance(n, str) and NAME.fullmatch(n) for n in nodes), "invalid target nodes")
    require(len(set(nodes)) == len(nodes) and node in nodes, "artifact node mismatch")
    roles = manifest["node_roles"]
    require(isinstance(roles, dict) and set(roles) == set(nodes) and all(r in ROLES for r in roles.values()), "artifact node roles mismatch")
    operations = manifest["operations"]
    require(isinstance(operations, list) and operations and all(isinstance(o, str) and o in OPERATIONS for o in operations) and len(set(operations)) == len(operations), "invalid artifact operations")
    files, images, configurations, secrets = (manifest[k] for k in ("files", "image_digests", "config_hashes", "required_secrets"))
    require(isinstance(files, dict) and files and all(isinstance(h, str) and SHA256.fullmatch(h) for h in files.values()), "invalid file hashes")
    for name in files:
        canonical_path(name)
    require("bin/runtime-entrypoint.py" in files, "fixed runtime entrypoint required")
    require(isinstance(images, dict) and all(isinstance(k, str) and k and isinstance(v, str) and re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", v) for k, v in images.items()), "immutable image digest binding required")
    require(isinstance(configurations, dict) and set(configurations) <= set(files) and all(files[k] == h for k, h in configurations.items()), "configuration hashes mismatch")
    require(isinstance(secrets, dict) and set(secrets) == set(nodes), "node secret bindings required")
    for paths in secrets.values():
        require(isinstance(paths, list) and all(isinstance(p, str) and p.startswith("/run/mlo/secrets/") and str(PurePosixPath(p)) == p and ".." not in PurePosixPath(p).parts for p in paths), "unsafe secret reference")
        require(len(set(paths)) == len(paths), "duplicate secret reference")
    return manifest


def inspect_artifact(archive, expected_sha256, environment, generation, node, destination, expected_manifest_sha256=None):
    archive, destination = Path(archive), Path(destination)
    require(isinstance(expected_sha256, str) and SHA256.fullmatch(expected_sha256), "accepted archive SHA256 required")
    require(isinstance(expected_manifest_sha256, str) and SHA256.fullmatch(expected_manifest_sha256), "accepted manifest SHA256 required")
    require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= MAX_TOTAL_BYTES, "invalid or oversized archive")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    require(digest.hexdigest() == expected_sha256, "artifact checksum mismatch")
    try:
        with gzip.open(archive, "rb") as stream:
            expanded = stream.read(MAX_TAR_BYTES + 1)
        require(len(expanded) <= MAX_TAR_BYTES, "expanded archive too large")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as payload:
            members, names, total = [], set(), 0
            for member in payload:
                canonical_path(member.name)
                require(member.isfile() and not member.issym() and not member.islnk(), "artifact requires regular files only")
                require(member.name not in names and 0 <= member.size <= MAX_FILE_BYTES, "duplicate or oversized artifact member")
                total += member.size
                require(total <= MAX_TOTAL_BYTES and len(members) < MAX_MEMBERS, "artifact too large")
                names.add(member.name)
                members.append(member)
            require("manifest.json" in names, "manifest required")
            raw_manifest = payload.extractfile("manifest.json").read()
            require(hashlib.sha256(raw_manifest).hexdigest() == expected_manifest_sha256, "manifest checksum mismatch")
            manifest = validate_manifest(json.loads(raw_manifest, object_pairs_hook=unique_json), environment, generation, node)
            require(set(manifest["files"]) == names - {"manifest.json"}, "manifest file coverage mismatch")
            for name, expected in manifest["files"].items():
                require(hashlib.sha256(payload.extractfile(name).read()).hexdigest() == expected, "artifact file checksum mismatch")
            # Validation finishes before creating a published destination or extracting any file.
            require(not destination.exists() and not destination.is_symlink(), "destination must be a new version directory")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent = destination.parent.resolve()
            destination = parent / destination.name
            lock = parent / ("." + destination.name + ".artifact.lock")
            try:
                descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ContractError("artifact destination is locked") from exc
            os.close(descriptor)
            staging = None
            try:
                require(not destination.exists() and not destination.is_symlink(), "destination already exists")
                staging = Path(tempfile.mkdtemp(prefix=".artifact-", dir=parent))
                for member in members:
                    target = staging / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with target.open("xb") as stream:
                        stream.write(payload.extractfile(member).read())
                        stream.flush()
                        os.fsync(stream.fileno())
                    target.chmod(0o600)
                os.rename(staging, destination)
                staging = None
                descriptor = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                if staging is not None:
                    shutil.rmtree(staging)
                lock.unlink(missing_ok=True)
    except (gzip.BadGzipFile, tarfile.TarError, EOFError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("invalid gzip TAR or manifest") from exc
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("archive", "sha256", "manifest-sha256", "environment", "generation", "node", "destination"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args()
    try:
        inspect_artifact(args.archive, args.sha256, args.environment, args.generation, args.node, args.destination, args.manifest_sha256)
    except (ContractError, OSError) as exc:
        parser.exit(2, "artifact refused: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
