"""Local demonstration storage only. Never reads or changes a V1 database."""
import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from .errors import AnalyticsError, CapacityExceeded, Conflict, LeaseLost
from .events import Event, canonical


@dataclass(frozen=True)
class Claim:
    event: Event
    lease_token: str
    attempt: int


def validate_claim(now, lease_seconds, max_attempts):
    if (type(now) not in (int, float) or not math.isfinite(now) or now < 0
            or type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds)
            or not 0 < lease_seconds <= 300 or type(max_attempts) is not int
            or not 1 <= max_attempts <= 100):
        raise AnalyticsError("INVALID_LEASE_BOUNDS")


class SQLiteStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS events(
                event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                state TEXT NOT NULL CHECK(state IN ('PENDING','SENDING','SENT','DEAD')),
                due_at REAL NOT NULL, lease_token TEXT, lease_until REAL,
                attempts INTEGER NOT NULL DEFAULT 0, error_code TEXT);
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state,due_at,lease_until);
            CREATE TABLE IF NOT EXISTS reports(report_id TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def ingest(self, events, *, enqueue=False, now=0.0):
        added = 0
        with self.transaction():
            for event in events:
                old = self.connection.execute("SELECT fingerprint FROM events WHERE event_id=?",
                                              (event.event_id,)).fetchone()
                if old and old[0] != event.fingerprint:
                    raise Conflict("EVENT_ID_CONFLICT")
                if not old:
                    self.connection.execute("INSERT INTO events VALUES(?,?,?)",
                                            (event.event_id, event.fingerprint, canonical(event.envelope())))
                    added += 1
                if enqueue:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO outbox(event_id,state,due_at) VALUES(?,'PENDING',?)",
                        (event.event_id, now))
        return added

    def read_events(self, *, limit=100_000):
        rows = self.connection.execute("SELECT body FROM events ORDER BY event_id LIMIT ?", (limit + 1,)).fetchall()
        if len(rows) > limit:
            raise CapacityExceeded("REPORT_EVENT_LIMIT")
        return [Event.from_envelope(json.loads(row[0])) for row in rows]

    def claim(self, *, now, lease_seconds=60, max_attempts=8):
        validate_claim(now, lease_seconds, max_attempts)
        with self.transaction():
            due = "((state='PENDING' AND due_at<=?) OR (state='SENDING' AND lease_until<=?))"
            self.connection.execute(f"UPDATE outbox SET state='DEAD' WHERE attempts>=? AND {due}",
                                    (max_attempts, now, now))
            row = self.connection.execute(
                f"SELECT event_id,attempts FROM outbox WHERE attempts<? AND {due} ORDER BY due_at,event_id LIMIT 1",
                (max_attempts, now, now)).fetchone()
            if not row:
                return None
            token = uuid4().hex
            self.connection.execute(
                "UPDATE outbox SET state='SENDING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE event_id=?",
                (token, now + lease_seconds, row[0]))
            body = self.connection.execute("SELECT body FROM events WHERE event_id=?", (row[0],)).fetchone()[0]
            return Claim(Event.from_envelope(json.loads(body)), token, row[1] + 1)

    def _finish(self, claim, now, *, success, delay=0, code=None):
        with self.transaction():
            count = self.connection.execute(
                "UPDATE outbox SET state=?,due_at=?,lease_token=NULL,lease_until=NULL,error_code=? "
                "WHERE event_id=? AND state='SENDING' AND lease_token=? AND lease_until>?",
                ("SENT" if success else "PENDING", now + delay, code, claim.event.event_id,
                 claim.lease_token, now)).rowcount
            if count != 1:
                raise LeaseLost("OUTBOX_LEASE_LOST")

    def acknowledge(self, claim, *, now):
        self._finish(claim, now, success=True)

    def release(self, claim, *, now, delay, code="BROKER_SEND_FAILED"):
        self._finish(claim, now, success=False, delay=delay, code=code)

    def queue_counts(self):
        return dict(self.connection.execute("SELECT state,COUNT(*) FROM outbox GROUP BY state"))

    def save_report(self, report):
        body = canonical(report)
        with self.transaction():
            old = self.connection.execute("SELECT body FROM reports WHERE report_id=?",
                                          (report["report_id"],)).fetchone()
            if old and old[0] != body:
                raise Conflict("REPORT_ID_CONFLICT")
            self.connection.execute("INSERT OR IGNORE INTO reports VALUES(?,?)", (report["report_id"], body))

    def close(self):
        self.connection.close()
