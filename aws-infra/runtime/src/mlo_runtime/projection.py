"""Cursor/heartbeat plumbing around an accepted observation export artifact.

The source adapter is supplied by the app/ads team. No source business database
or embedded log_record is interpreted or updated by this runtime component.
"""

import datetime
import json
from pathlib import Path
import time
import uuid

from .observability import sanitize
from .operations import UnsafeTransition, atomic_json, local_lock


class ProjectionRunner:
    def __init__(self, state, spool, exporter, guard, heartbeat, *, clock=time.time):
        self.state, self.spool, self.exporter, self.guard = (
            Path(state),
            spool,
            exporter,
            guard,
        )
        self.heartbeat, self.clock = sanitize(heartbeat), clock

    def step(self):
        with local_lock(str(self.state) + ".lock"):
            if self.guard() is not True:
                raise UnsafeTransition("projection execution admission rejected")
            previous = (
                json.loads(self.state.read_text())
                if self.state.exists()
                else {"cursor": None, "last_progress": self.clock(), "attempt": 0}
            )
            marker = dict(self.heartbeat)
            marker["pipeline"] = {
                **marker["pipeline"],
                "state": "HEARTBEAT",
                "attempt": previous["attempt"] + 1,
            }
            marker["event"] = {
                **marker["event"],
                "id": str(uuid.uuid4()),
                "outcome": "unknown",
            }
            marker["@timestamp"] = datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat()
            try:
                result = self.exporter(previous["cursor"], limit=128)
                if (
                    set(result) != {"events", "cursor", "backlog"}
                    or not isinstance(result["events"], list)
                    or len(result["events"]) > 128
                    or type(result["backlog"]) is not int
                    or result["backlog"] < 0
                    or not isinstance(result["cursor"], (str, type(None)))
                    or (result["cursor"] is not None and len(result["cursor"]) > 1024)
                ):
                    raise UnsafeTransition("invalid bounded observation export result")
                events = [sanitize(event) for event in result["events"]]
                for event in events:
                    self.spool.append(event)
                progressed = result["cursor"] != previous["cursor"]
                now = self.clock()
                state = {
                    "cursor": result["cursor"],
                    "last_progress": now if progressed else previous["last_progress"],
                    "attempt": previous["attempt"] + 1,
                    "backlog": result["backlog"],
                }
                if (
                    not progressed
                    and result["backlog"] > 0
                    and now - state["last_progress"] >= 120
                ):
                    marker["pipeline"]["state"] = "STALE"
                self.spool.append(marker)
                # Cursor commits only after every observation reached durable spool.
                # Crash replay retains source event IDs; sink dedupes event.id.
                atomic_json(self.state, state)
                return {
                    "state": marker["pipeline"]["state"],
                    "records": len(events),
                    "backlog": result["backlog"],
                }
            except Exception:
                marker["pipeline"]["state"] = "RETRYING"
                try:
                    self.spool.append(marker)
                except UnsafeTransition:
                    pass
                raise
