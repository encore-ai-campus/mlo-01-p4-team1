#!/usr/bin/env python3
"""Install pinned provider-free Terraform in C cache. Separate from test/execution."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import stat
import urllib.request
import zipfile

BASE = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    lock = json.loads((BASE / "toolchain.lock.json").read_text())["terraform"]
    target = {("Darwin", "arm64"): "darwin_arm64", ("Linux", "x86_64"): "linux_amd64"}.get((platform.system(), platform.machine()))
    if target not in lock["archives"]:
        raise SystemExit("NOT READY: supported hosts are darwin_arm64/linux_amd64")
    folder = BASE / ".cache/toolchain"
    folder.mkdir(parents=True, exist_ok=True)
    archive = args.archive or folder / ("terraform_1.11.4_" + target + ".zip")
    if args.archive is None:
        with urllib.request.urlopen(lock["source"] + archive.name, timeout=60) as response:
            raw = response.read(64 * 1024 * 1024)
        archive.write_bytes(raw)
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != lock["archives"][target]:
        raise SystemExit("NOT READY: Terraform archive checksum mismatch")
    with zipfile.ZipFile(archive) as payload:
        entries = payload.infolist()
        if any(e.filename not in {"terraform", "LICENSE.txt"} or e.is_dir() or e.file_size > 128 * 1024 * 1024
               or stat.S_IFMT(e.external_attr >> 16) not in {0, stat.S_IFREG} for e in entries):
            raise SystemExit("NOT READY: unexpected archive members")
        binary = payload.read("terraform")
    path = folder / "terraform"
    if path.is_symlink():
        raise SystemExit("NOT READY: symlink output refused")
    path.write_bytes(binary)
    path.chmod(0o755)
    print("PASS: C-only Terraform 1.11.4; no provider/backend access")


if __name__ == "__main__":
    main()
