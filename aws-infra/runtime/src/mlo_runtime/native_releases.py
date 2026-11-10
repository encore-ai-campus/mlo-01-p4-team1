"""Pinned native archives: offline verification/extraction, optional explicit fetch.

No package manager, shell hook, service command or AWS operation is executed.
Native archives stay separate from the small manifest-v1 configuration artifact.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


def need(ok, message):
    if not ok:
        raise ValueError(message)


def file_digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def lock(path):
    from .install import read
    value = read(path)
    need(value["schema_version"] == "native-releases-v1" and value["platform"] == "linux/amd64", "native lock identity")
    need(set(value["releases"]) == {"kafka", "spark", "zookeeper", "elasticsearch", "java21"}, "native release closure")
    for name, item in value["releases"].items():
        need(item["algorithm"] in {"sha256", "sha512"}, "native hash algorithm")
        need(re.fullmatch("[a-f0-9]{" + str(64 if item["algorithm"] == "sha256" else 128) + "}", item["digest"]), "native digest")
        need(item["url"].startswith(("https://archive.apache.org/dist/", "https://artifacts.elastic.co/downloads/", "https://github.com/adoptium/temurin21-binaries/releases/download/")) and "/latest/" not in item["url"], "untrusted/mutable native source")
        need(re.fullmatch(r"[A-Za-z0-9_.+-]+", item["root"]), "native root")
        need(item["executables"] and all(safe_path(p) for p in item["executables"]), "native executables")
    return value


def safe_path(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and "\\" not in name and "\x00" not in name


def archive_path(cache, name, item):
    return Path(cache) / (name + "-" + item["digest"][:16] + ".tar.gz")


def verify(path, item):
    path = Path(path)
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2 * 1024**3, "native archive missing/oversized")
    need(file_digest(path, item["algorithm"]) == item["digest"], "native archive checksum mismatch")
    return {"sha256": file_digest(path), "bytes": path.stat().st_size, "publisher_digest": item["digest"]}


def fetch(cache, name, item):
    target = archive_path(cache, name, item)
    if target.exists():
        return verify(target, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".native-download-", dir=target.parent)
    os.close(fd)
    try:
        subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https", "--proto-redir", "=https", "--max-time", "600", "--output", temporary, item["url"]], check=True, timeout=610)
        result = verify(temporary, item)
        os.link(temporary, target)  # no replacement of an existing accepted archive
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)


def extract(archive, item, destination):
    """Only regular files/directories; safe internal symlinks are copied as files.

    JDK/ES distributions contain internal links. Resolve them in the archive,
    never on the host; refuse hardlinks, devices, escaping paths or link chains.
    """
    verify(archive, item)
    destination = Path(destination)
    need(not destination.exists() and not destination.is_symlink(), "native destination exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".native-extract-", dir=destination.parent))
    try:
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            need(len(members) <= 100000 and sum(m.size for m in members) <= 6 * 1024**3, "native archive expansion limit")
            by_name = {}
            for m in members:
                name = m.name.rstrip("/")
                need(safe_path(name) and PurePosixPath(name).parts[0] == item["root"] and name not in by_name, "native archive path/duplicate")
                need(m.isdir() or m.isfile() or m.issym(), "native special/hardlink member refused")
                by_name[name] = m
            for name, member in by_name.items():
                relative = PurePosixPath(name).relative_to(item["root"])
                if str(relative) == ".":
                    continue
                target = staging / str(relative)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                original = member
                if member.issym():
                    # Normalize without consulting the host filesystem.
                    need(not member.linkname.startswith("/"), "absolute native symlink")
                    pieces = list(PurePosixPath(name).parent.parts)
                    for part in PurePosixPath(member.linkname).parts:
                        if part == "..":
                            need(len(pieces) > 1, "escaping native symlink")
                            pieces.pop()
                        elif part != ".":
                            pieces.append(part)
                    original = by_name.get("/".join(pieces))
                    need(original is not None and original.isfile(), "native link target must be regular archive file")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(original) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if original.mode & 0o111 else 0o644)
        for name in item["executables"]:
            p = staging / name
            need(p.is_file() and p.stat().st_mode & 0o111, "native executable missing")
        staging.chmod(0o755)
        os.rename(staging, destination)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["verify", "fetch"])
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = lock(args.lock)
    result = {"schema_version": "native-archives-receipt-v1", "lock_sha256": file_digest(args.lock), "ready_for_aws": False, "aws_calls": False, "releases": {}}
    for name, item in value["releases"].items():
        result["releases"][name] = fetch(args.cache, name, item) if args.operation == "fetch" else verify(archive_path(args.cache, name, item), item)
        print("verified " + name, flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
