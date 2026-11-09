"""Local policy-worker heartbeat. No database or HTTP calls from this health check."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def write_health(snapshot, *, filename=None, now=None):
    filename = os.environ.get("ADS_WORKER_HEALTH_FILE", "") if filename is None else filename
    if not filename:
        return
    target = Path(filename)
    state = {"written_at": time.time() if now is None else now, "jobs": snapshot}
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(target)


def healthy(state, *, now=None):
    now = time.time() if now is None else now
    try:
        if not 0 <= now - state["written_at"] <= 65:
            return False
        policy = state["jobs"]["policy"]
        if policy["status"] not in ("OK", "RUNNING") or not policy.get("last_succeeded_at"):
            return False
        succeeded = datetime.fromisoformat(policy["last_succeeded_at"].replace("Z", "+00:00"))
        return succeeded.tzinfo is not None and 0 <= now - succeeded.timestamp() <= 360
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def main():
    filename = os.environ.get("ADS_WORKER_HEALTH_FILE", "")
    try:
        path = Path(filename)
        if not filename or not path.is_file() or path.stat().st_size > 65536:
            return 1
        return 0 if healthy(json.loads(path.read_text(encoding="utf-8"))) else 1
    except (OSError, ValueError, TypeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
