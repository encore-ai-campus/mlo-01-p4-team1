"""Remove privacy-risk PNG ancillary metadata without re-encoding image pixels.

Usage:
  python3 docker/sanitize_png_metadata.py           # audit Git-eligible PNGs
  python3 docker/sanitize_png_metadata.py --apply   # strip metadata losslessly

Does not inspect personal data embedded in visible pixels; visually review screenshots.
"""
from pathlib import Path
import argparse
import hashlib
import os
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
SIGNATURE = b"\x89PNG\r\n\x1a\n"
DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}


def sanitize(path):
    original = path.read_bytes()
    if not original.startswith(SIGNATURE):
        raise ValueError(f"Not a PNG: {path}")
    at = len(SIGNATURE)
    kept = bytearray(SIGNATURE)
    removed = []
    source_idat = hashlib.sha256()
    new_idat = hashlib.sha256()
    terminated = False
    while at + 12 <= len(original):
        size = struct.unpack(">I", original[at:at + 4])[0]
        end = at + size + 12
        if end > len(original):
            raise ValueError(f"Truncated PNG: {path}")
        kind = original[at + 4:at + 8]
        content = original[at + 8:at + 8 + size]
        expected_crc = struct.unpack(">I", original[at + 8 + size:end])[0]
        if zlib.crc32(kind + content) != expected_crc:
            raise ValueError(f"Invalid CRC: {path}")
        if kind == b"IDAT":
            source_idat.update(content)
        if kind in DROP:
            removed.append(kind.decode("ascii"))
        else:
            kept.extend(original[at:end])
            if kind == b"IDAT":
                new_idat.update(content)
        at = end
        if kind == b"IEND":
            terminated = True
            break
    if not terminated or at != len(original):
        raise ValueError(f"Invalid PNG end: {path}")
    if source_idat.digest() != new_idat.digest():
        raise AssertionError("Image data must not change")
    return bytes(kept), removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "app"],
        cwd=ROOT,
    )
    files = sorted(
        {ROOT / os.fsdecode(name) for name in data.split(b"\0")
         if name and name.lower().endswith(b".png")}
    )
    changed = 0
    for path in files:
        if not path.is_file():
            continue
        cleaned, removed = sanitize(path)
        if not removed:
            continue
        changed += 1
        label = str(path.relative_to(ROOT))
        print("PNG_METADATA:", label, sorted(set(removed)), "bytes_removed", path.stat().st_size - len(cleaned))
        if args.apply:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".sanitized-png-", delete=False) as tmp:
                tmp.write(cleaned)
                tmp_path = Path(tmp.name)
            try:
                tmp_path.chmod(path.stat().st_mode & 0o777)
                os.replace(tmp_path, path)
            finally:
                tmp_path.unlink(missing_ok=True)
    print("total_png", len(files), "with_risk_metadata", changed, "applied", args.apply)


if __name__ == "__main__":
    main()
