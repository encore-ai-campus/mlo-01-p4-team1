"""Verify a fully SHA-pinned offline feedback Python wheel set (no installation).

The lock is reviewed source metadata; raw wheels live only in ignored/private
storage. A passing local check does NOT accept a production Control host.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from .native_releases import need

HEX = re.compile(r"[0-9a-f]{64}\Z")
REQ = re.compile(r"([A-Za-z0-9._-]+)==([A-Za-z0-9._+!-]+)\Z")
BASE = re.compile(r"[A-Za-z0-9_.-]+\.whl\Z")
TARGET_DIRECT = {
    "django", "djangorestframework", "psycopg", "psycopg-binary",
    "argon2-cffi", "cryptography", "kafka-python", "requests",
    "boto3", "authlib",
}


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def requirements(data):
    pins = {}
    for raw in data.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = REQ.fullmatch(line)
        need(match is not None, "backend dependency lock contains an unpinned requirement")
        name = canonical(match[1])
        need(name not in pins, "duplicate backend requirement")
        pins[name] = match[2]
    return pins


def compatible(tags):
    """Allow native CPython 3.12 and stable ABI3 Linux/amd64, or pure Python."""
    for tag in tags:
        parts = tag.split("-")
        if len(parts) != 3:
            continue
        interpreter, abi, platform = parts
        if platform == "any":
            if abi == "none" and interpreter in {"py3", "py2.py3", "py312"}:
                return True
            continue
        platforms = platform.split(".")
        if not platforms or not all("x86_64" in p for p in platforms):
            continue
        # The target image is Ubuntu 24.04/glibc 2.39.
        allowed = any(
            (m := re.fullmatch(r"manylinux_2_(\d+)_x86_64", p))
            and int(m[1]) <= 39
            or p == "manylinux2014_x86_64"
            for p in platforms
        )
        if not allowed:
            continue
        if interpreter == "cp312" and abi in {"cp312", "abi3"}:
            return True
        if abi == "abi3" and interpreter.startswith("cp3"):
            try:
                if 2 <= int(interpreter[3:]) <= 12:
                    return True
            except ValueError:
                pass
    return False


def verify(wheelhouse, lockfile, backend_lock, app_zip=None):
    wheelhouse, lockfile, backend_lock = map(Path, (wheelhouse, lockfile, backend_lock))
    need(lockfile.is_file() and not lockfile.is_symlink(), "native feedback Python lock missing")
    need(backend_lock.is_file() and not backend_lock.is_symlink(), "backend dependency source lock missing")
    record = json.loads(lockfile.read_text())
    need(record.get("schema_version") == "native-feedback-python-lock-v1"
         and record.get("os") == "ubuntu-24.04"
         and record.get("python") == "CPython-3.12"
         and record.get("architecture") == "x86_64"
         and record.get("ready_for_aws") is False
         and record.get("unity_catalog_excluded") is True,
         "native Python target or acceptance contract mismatch")
    need(record.get("source_requirements_sha256") == digest(backend_lock),
         "backend dependency lock hash mismatch")
    approved = requirements(backend_lock.read_bytes())
    direct = {}
    for item in record.get("direct_requirements", []):
        match = REQ.fullmatch(item)
        need(match is not None, "invalid direct feedback requirement")
        key = canonical(match[1])
        need(key not in direct and approved.get(key) == match[2], "unapproved direct feedback pin")
        direct[key] = match[2]
    need(set(direct) == TARGET_DIRECT, "incomplete or unreviewed direct feedback dependency set")
    entries = record.get("wheels")
    need(isinstance(entries, list) and entries, "empty offline wheel closure")
    need(wheelhouse.is_dir() and not wheelhouse.is_symlink(), "wheelhouse directory invalid")
    names, packages, hashes = set(), set(), {}
    for entry in entries:
        need(isinstance(entry, dict) and set(entry) == {
             "name", "version", "filename", "sha256", "bytes"}, "unreviewed wheel metadata")
        name, version, filename, sha, size = (
            entry[k] for k in ("name", "version", "filename", "sha256", "bytes"))
        need(isinstance(name, str) and isinstance(filename, str)
             and isinstance(version, str) and canonical(name) == name
             and approved.get(name) == version
             and isinstance(sha, str) and bool(HEX.fullmatch(sha))
             and isinstance(size, int) and 100 <= size <= 200_000_000
             and BASE.fullmatch(filename)
             and not filename.startswith(".")
             and filename not in names and name not in packages,
             "unknown or duplicated feedback wheel pin")
        # Wheel filename tags must agree with the target even when embedded
        # metadata claims a compatible interpreter/ABI.
        wheel_parts = filename[:-4].rsplit("-", 3)
        need(len(wheel_parts) == 4
             and compatible(["-".join(wheel_parts[1:])]),
             "wheel filename targets an unapproved Python ABI/platform")
        names.add(filename)
        packages.add(name)
        src = wheelhouse / filename
        need(src.is_file() and not src.is_symlink() and src.stat().st_size == size
             and digest(src) == sha, "native feedback Python wheel changed/missing")
        # Inspect metadata; SHA alone is insufficient to catch a malformed lock
        # pointing at an unrelated wheel or at an incompatible CPython ABI.
        with zipfile.ZipFile(src) as archive:
            info = [p for p in archive.namelist() if p.endswith(".dist-info/METADATA")]
            wheel = [p for p in archive.namelist() if p.endswith(".dist-info/WHEEL")]
            need(len(info) == len(wheel) == 1, "wheel metadata missing/ambiguous")
            metadata = archive.read(info[0]).decode("utf-8")
            wheel_text = archive.read(wheel[0]).decode("utf-8")
        recorded_name = next((s[6:].strip() for s in metadata.splitlines() if s.startswith("Name: ")), "")
        recorded_version = next((s[9:].strip() for s in metadata.splitlines() if s.startswith("Version: ")), "")
        need(canonical(recorded_name) == name and recorded_version == version,
             "wheel metadata name/version does not match trusted lock")
        tags = [s[5:].strip() for s in wheel_text.splitlines() if s.startswith("Tag: ")]
        need(compatible(tags), "wheel does not support Ubuntu 24.04 CPython 3.12 AMD64")
        hashes[name] = sha
    actual_names = {p.name for p in wheelhouse.iterdir()}
    need(actual_names == names, "unreviewed wheelhouse file or missing wheel")
    need(set(direct) <= packages
         and "typing-extensions" in packages,
         "incomplete CPython 3.12 offline dependency closure")
    if app_zip is not None:
        with zipfile.ZipFile(app_zip) as archive:
            need("backend/requirements.lock" in archive.namelist()
                 and hashlib.sha256(archive.read("backend/requirements.lock")).hexdigest()
                 == record["source_requirements_sha256"],
                 "Spark shared app zip and offline Python requirements disagree")
    return {"lock_sha256": digest(lockfile),
            "source_requirements_sha256": record["source_requirements_sha256"],
            "wheel_count": len(entries), "wheel_names": sorted(packages),
            "ready_for_aws": False}


def hashed_requirements(lockfile):
    record = json.loads(Path(lockfile).read_text())
    return "".join(
        "%s==%s --hash=sha256:%s\n" % (x["name"], x["version"], x["sha256"])
        for x in sorted(record["wheels"], key=lambda x: x["name"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--wheelhouse", required=True, type=Path)
    parser.add_argument("--backend-lock", required=True, type=Path)
    parser.add_argument("--app-zip", type=Path)
    parser.add_argument("--hashed-output", type=Path)
    a = parser.parse_args()
    verified = verify(a.wheelhouse, a.lock, a.backend_lock, a.app_zip)
    if a.hashed_output is not None:
        need(not a.hashed_output.exists() and not a.hashed_output.is_symlink()
             and ".local" in a.hashed_output.parts,
             "hashed install lock must use a fresh ignored local file")
        a.hashed_output.write_text(hashed_requirements(a.lock))
    print(json.dumps({k: v for k, v in verified.items() if k != "wheel_names"}, sort_keys=True))


if __name__ == "__main__":
    main()
