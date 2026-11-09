"""Durable isolated V2 events, capture progress, outbox and report snapshots."""

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .errors import AnalyticsError, CapacityExceeded, Conflict, LeaseLost
from .events import Event, canonical, stamp


@dataclass(frozen=True)
class Claim:
    event: Event
    lease_token: str
    attempt: int


def validate_claim(now, lease_seconds, max_attempts):
    if (
        type(now) not in (int, float)
        or not math.isfinite(now)
        or now < 0
        or type(lease_seconds) not in (int, float)
        or not math.isfinite(lease_seconds)
        or not 0 < lease_seconds <= 300
        or type(max_attempts) is not int
        or not 1 <= max_attempts <= 100
    ):
        raise AnalyticsError("INVALID_LEASE_BOUNDS")


def validate_requeue(event_ids, now):
    values = list(event_ids)
    validate_claim(now, 60, 8)
    if (
        not values
        or len(values) > 1000
        or not all(isinstance(value, str) and value for value in values)
        or len(set(values)) != len(values)
    ):
        raise AnalyticsError("INVALID_REQUEUE_IDS")
    return values


class SQLiteStore:
    def __init__(self, path):
        self.storage_identity = (
            ("sqlite", str(Path(path).resolve()))
            if str(path) != ":memory:"
            else ("memory", id(self))
        )
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
            CREATE TABLE IF NOT EXISTS captures(
                source_id TEXT PRIMARY KEY, version INTEGER NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox_requeues(
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
                previous_attempts INTEGER NOT NULL, requeued_at REAL NOT NULL);
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

    def _ingest(self, events, *, enqueue, now):
        added = 0
        for event in events:
            old = self.connection.execute(
                "SELECT fingerprint FROM events WHERE event_id=?", (event.event_id,)
            ).fetchone()
            if old and old[0] != event.fingerprint:
                raise Conflict("EVENT_ID_CONFLICT")
            if not old:
                self.connection.execute(
                    "INSERT INTO events VALUES(?,?,?)",
                    (event.event_id, event.fingerprint, canonical(event.envelope())),
                )
                added += 1
            if enqueue:
                self.connection.execute(
                    "INSERT OR IGNORE INTO outbox(event_id,state,due_at) VALUES(?,'PENDING',?)",
                    (event.event_id, now),
                )
        return added

    def ingest(self, events, *, enqueue=False, now=0.0):
        with self.transaction():
            return self._ingest(events, enqueue=enqueue, now=now)

    def capture_state(self, source_id):
        row = self.connection.execute(
            "SELECT version,body FROM captures WHERE source_id=?", (source_id,)
        ).fetchone()
        return {**json.loads(row["body"]), "version": row["version"]} if row else None

    def ingest_capture(self, events, *, source_id, expected_version, state, now=0):
        """Progress and the entire validated page commit together; stale collectors fail."""
        with self.transaction():
            row = self.connection.execute(
                "SELECT version FROM captures WHERE source_id=?", (source_id,)
            ).fetchone()
            version = row[0] if row else 0
            if version != expected_version:
                raise Conflict("CAPTURE_STATE_CHANGED")
            added = self._ingest(events, enqueue=True, now=now)
            self.connection.execute(
                "INSERT OR REPLACE INTO captures VALUES(?,?,?)",
                (source_id, version + 1, canonical(state)),
            )
        return added

    def read_events(self, *, limit=100_000):
        rows = self.connection.execute(
            "SELECT body FROM events ORDER BY event_id LIMIT ?", (limit + 1,)
        ).fetchall()
        if len(rows) > limit:
            raise CapacityExceeded("REPORT_EVENT_LIMIT")
        return [Event.from_envelope(json.loads(row[0])) for row in rows]

    def claim(self, *, now, lease_seconds=60, max_attempts=8):
        validate_claim(now, lease_seconds, max_attempts)
        with self.transaction():
            due = "((state='PENDING' AND due_at<=?) OR (state='SENDING' AND lease_until<=?))"
            self.connection.execute(
                f"UPDATE outbox SET state='DEAD' WHERE attempts>=? AND {due}",
                (max_attempts, now, now),
            )
            row = self.connection.execute(
                f"SELECT event_id,attempts FROM outbox WHERE attempts<? AND {due} ORDER BY due_at,event_id LIMIT 1",
                (max_attempts, now, now),
            ).fetchone()
            if not row:
                return None
            token = uuid4().hex
            self.connection.execute(
                "UPDATE outbox SET state='SENDING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE event_id=?",
                (token, now + lease_seconds, row[0]),
            )
            body = self.connection.execute(
                "SELECT body FROM events WHERE event_id=?", (row[0],)
            ).fetchone()[0]
            return Claim(Event.from_envelope(json.loads(body)), token, row[1] + 1)

    def _finish(self, claim, now, *, success, delay=0, code=None):
        with self.transaction():
            count = self.connection.execute(
                "UPDATE outbox SET state=?,due_at=?,lease_token=NULL,lease_until=NULL,error_code=? "
                "WHERE event_id=? AND state='SENDING' AND lease_token=? AND lease_until>?",
                (
                    "SENT" if success else "PENDING",
                    now + delay,
                    code,
                    claim.event.event_id,
                    claim.lease_token,
                    now,
                ),
            ).rowcount
            if count != 1:
                raise LeaseLost("OUTBOX_LEASE_LOST")

    def acknowledge(self, claim, *, now):
        self._finish(claim, now, success=True)

    def release(self, claim, *, now, delay, code="BROKER_SEND_FAILED"):
        self._finish(claim, now, success=False, delay=delay, code=code)

    def queue_counts(self):
        return dict(self.connection.execute("SELECT state,COUNT(*) FROM outbox GROUP BY state"))

    def dead_letters(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AnalyticsError("INVALID_DEAD_LETTER_LIMIT")
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT event_id,attempts,error_code FROM outbox WHERE state='DEAD' ORDER BY event_id LIMIT ?",
                (limit,),
            )
        ]

    def requeue_dead(self, event_ids, *, now):
        values = validate_requeue(event_ids, now)
        with self.transaction():
            rows = []
            for identity in values:
                row = self.connection.execute(
                    "SELECT attempts,state FROM outbox WHERE event_id=?", (identity,)
                ).fetchone()
                if row is None or row["state"] != "DEAD":
                    raise Conflict("REQUEUE_REQUIRES_DEAD")
                rows.append((identity, row["attempts"], now))
            self.connection.executemany(
                "INSERT INTO outbox_requeues(event_id,previous_attempts,requeued_at) VALUES(?,?,?)",
                rows,
            )
            self.connection.executemany(
                "UPDATE outbox SET state='PENDING',due_at=?,attempts=0,lease_token=NULL,"
                "lease_until=NULL,error_code=NULL WHERE event_id=?",
                [(now, value) for value in values],
            )
        return len(values)

    def save_report(self, report):
        body = canonical(report)
        with self.transaction():
            old = self.connection.execute(
                "SELECT body FROM reports WHERE report_id=?", (report["report_id"],)
            ).fetchone()
            if old and old[0] != body:
                raise Conflict("REPORT_ID_CONFLICT")
            self.connection.execute(
                "INSERT OR IGNORE INTO reports VALUES(?,?)", (report["report_id"], body)
            )

    def latest_report(self, cutoff=None):
        """Only full materialized snapshots, never a fresh computation or filtered CLI report."""
        bound = stamp(cutoff) if cutoff is not None else "9999-12-31T23:59:59.999999Z"
        row = self.connection.execute(
            "SELECT body FROM reports WHERE json_extract(body,'$.scope')='{}' "
            "AND json_extract(body,'$.as_of')<=? ORDER BY json_extract(body,'$.as_of') DESC,rowid DESC LIMIT 1",
            (bound,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.connection.close()
