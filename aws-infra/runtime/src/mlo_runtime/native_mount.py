"""Every-start mount identity guard, executed directly by systemd (stdlib only)."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


def check(binding_path):
    path = Path(binding_path)
    if path != Path("/etc/mlo/host-binding.json") or path.is_symlink():
        raise ValueError("protected native binding location")
    s = path.stat()
    if s.st_uid != 0 or stat.S_IMODE(s.st_mode) & 0o077:
        raise ValueError("protected native binding owner/mode")
    binding = json.loads(path.read_text())
    if binding.get("lab") is True or binding.get("root", "/") != "/" or not binding.get("mount_uuid"):
        raise ValueError("real native mount binding required")
    for field, expected in (("TARGET", "/srv/mlo"), ("UUID", binding["mount_uuid"])):
        actual = subprocess.run(["/usr/bin/findmnt", "-rn", "-T", "/srv/mlo", "-o", field], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        if actual != expected:
            raise ValueError("native mount identity changed")
    if not os.path.ismount("/srv/mlo"):
        raise ValueError("native mount absent")


if __name__ == "__main__":
    check(sys.argv[1])
