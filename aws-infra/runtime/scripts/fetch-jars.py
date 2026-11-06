"""Download only hash-locked Maven artifacts; no AWS SDK or credentials."""

import hashlib
import json
from pathlib import Path
import urllib.request

runtime = Path(__file__).resolve().parents[1]
folder = runtime / ".artifacts/jars"
folder.mkdir(parents=True, exist_ok=True)
for item in json.loads((runtime / "connectors.lock.json").read_text())["jars"]:
    path = folder / item["artifact"]
    if (
        path.exists()
        and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
    ):
        continue
    with urllib.request.urlopen(item["source"], timeout=60) as response:
        contents = response.read(64 * 1024 * 1024)
    if hashlib.sha256(contents).hexdigest() != item["sha256"]:
        raise ValueError("Maven artifact hash mismatch: " + item["artifact"])
    path.write_bytes(contents)
    path.chmod(0o600)
