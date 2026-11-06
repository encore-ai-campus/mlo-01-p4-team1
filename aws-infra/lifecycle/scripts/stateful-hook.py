#!/usr/bin/env python3
"""Fixed local B adapter hook. SQLite only; never accepts shell/remote commands."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mlo_lifecycle.adapter import SQLiteStatefulBackend
from mlo_lifecycle.model import digest, need, read


def main():
    def audit(event, _):
        need(not event.startswith("socket.") and event not in {"subprocess.Popen", "os.system"}, "unexpected hook network/command")
    sys.addaudithook(audit)
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation", choices=["quiesce", "backup", "copy", "verify-copy", "verify-restore", "stop", "observe-stopped"], required=True)
    parser.add_argument("--node", choices=["postgresql", "mongodb"], required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--backup-sha256")
    parser.add_argument("--backup-location")
    args = parser.parse_args()
    lab = Path(os.environ["MLO_C_LAB"]).resolve()
    identity = read(lab / "identity.json")
    need(identity["example_only"] is True and identity["generation"] == args.generation, "non-local/mixed-generation hook refused")
    if "track2_sha256" in identity:
        from mlo_lifecycle.track2 import sealed, validate_spec
        spec = read(lab / "track2-spec.json")
        need(digest(spec) == identity["track2_sha256"], "Track2 hook input changed")
        validate_spec(spec, args.generation)
        sealed(lab, spec)
    backend = SQLiteStatefulBackend(lab, args.node, args.generation)
    operation = args.operation
    if operation in {"copy", "verify-copy", "verify-restore"}:
        previous = {"sha256": args.backup_sha256, "location": args.backup_location}
        method = {"copy": backend.copy_backup, "verify-copy": backend.verify_copy,
                  "verify-restore": backend.verify_restore}[operation]
        result = method(args.node, previous)
    elif operation == "observe-stopped":
        result = {"stopped": backend.observe_stopped(args.node)}
    else:
        result = getattr(backend, operation)(args.node) or {}
    print(json.dumps({**result, "node": args.node, "generation": args.generation, "operation": operation}))


if __name__ == "__main__":
    main()
