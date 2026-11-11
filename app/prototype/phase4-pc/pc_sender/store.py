"""Local atomic outbox; reception ACK does not imply DB publication."""

import math
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Conflict, SenderError, ValidationError
from .validation import canonical, validate_input

APPLICATION_ID = 0x5034534E


class LeaseLost(SenderError):
    pass


@dataclass(frozen=True)
class Claim:
    result_id: str
    payload_sha256: str
    body: bytes = field(repr=False)
    lease: str = field(repr=False)
    attempt: int = 0


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValidationError("INVALID_CLOCK")


class Outbox:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists() and not self.path.is_file():
            raise ValidationError("INVALID_OUTBOX_PATH")
        if self.path.exists() and self.path.stat().st_size:
            try:
                with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as existing:
                    if (existing.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                            or existing.execute("PRAGMA user_version").fetchone()[0] != 1):
                        raise ValidationError("NOT_SENDER_OUTBOX")
            except sqlite3.Error:
                raise ValidationError("NOT_SENDER_OUTBOX") from None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS sender_outbox (
            result_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL, body BLOB NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PENDING','SENDING','RETRY','RECEIVED','REJECTED','DEAD')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
            local_lease TEXT, lease_expires REAL, last_error TEXT,
            receipt_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
        )""")
        self.connection.execute("CREATE INDEX IF NOT EXISTS sender_ready ON sender_outbox(status,next_attempt)")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS sender_attempts (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL,
            state TEXT NOT NULL, attempt INTEGER NOT NULL, code TEXT,
            receipt_id TEXT, occurred_at REAL NOT NULL
        )""")
        self.connection.execute("CREATE INDEX IF NOT EXISTS sender_history ON sender_attempts(result_id,event_id)")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS worker_tasks (
            task_id TEXT PRIMARY KEY, input_key TEXT NOT NULL, attempts INTEGER NOT NULL,
            code TEXT, retryable INTEGER NOT NULL DEFAULT 0, retry_after REAL, result_id TEXT, updated_at REAL NOT NULL
        )""")
        self.connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        self.connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def enqueue(self, values, *, now: float, input_key: str | None = None) -> dict:
        _time(now)
        results = validate_input(values)
        if input_key is not None and len(results) != 1:
            raise ValidationError("INVALID_TASK_RESULT_COUNT")
        counts = {"inserted": 0, "existing": 0}
        with self._transaction():
            for result in results:
                body = canonical(result)
                existing = self.connection.execute(
                    "SELECT payload_sha256,body FROM sender_outbox WHERE result_id=?", (result["result_id"],)
                ).fetchone()
                if existing:
                    if existing["payload_sha256"] != result["payload_sha256"] or bytes(existing["body"]) != body:
                        raise Conflict("RESULT_ID_CONFLICT")
                    counts["existing"] += 1
                else:
                    self.connection.execute("""INSERT INTO sender_outbox
                        (result_id,payload_sha256,body,status,next_attempt,created_at,updated_at)
                        VALUES (?,?,?,'PENDING',?,?,?)""",
                        (result["result_id"], result["payload_sha256"], body, now, now, now))
                    self._record(result["result_id"], "PENDING", 0, now)
                    counts["inserted"] += 1
                if input_key is not None:
                    self.connection.execute("""INSERT INTO worker_tasks
                        (task_id,input_key,attempts,result_id,updated_at) VALUES (?,?,0,?,?)
                        ON CONFLICT(task_id) DO UPDATE SET input_key=excluded.input_key,attempts=0,
                        code=NULL,retryable=0,retry_after=NULL,result_id=excluded.result_id,updated_at=excluded.updated_at""",
                        (result["task_id"], input_key, result["result_id"], now))
        return counts

    def task_state(self, task_id: str, input_key: str) -> dict | None:
        row = self.connection.execute("""SELECT COALESCE(delivery.attempts,task.attempts) AS attempts,
            task.code,task.retryable,
            CASE WHEN delivery.status='RETRY' THEN delivery.next_attempt ELSE task.retry_after END AS retry_after,
            delivery.status AS delivery_status,delivery.last_error AS delivery_error
            FROM worker_tasks AS task LEFT JOIN sender_outbox AS delivery ON delivery.result_id=task.result_id
            WHERE task.task_id=? AND task.input_key=?""", (task_id, input_key)).fetchone()
        return dict(row) if row is not None else None

    def fail_task(self, task_id: str, input_key: str, code: str, *, now: float,
                  retry: bool, max_attempts: int, earliest_retry: float = 0) -> dict:
        _time(now)
        with self._transaction():
            previous = self.connection.execute("""SELECT attempts FROM worker_tasks
                WHERE task_id=? AND input_key=? AND result_id IS NULL""", (task_id, input_key)).fetchone()
            attempts = (previous["attempts"] if previous else 0) + 1
            retry_after = (max(min(earliest_retry, now + 3600), now + min(300, 15 * 2 ** (attempts - 1)))
                           if retry and attempts < max_attempts else None)
            self.connection.execute("""INSERT INTO worker_tasks
                (task_id,input_key,attempts,code,retryable,retry_after,updated_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET input_key=excluded.input_key,attempts=excluded.attempts,
                code=excluded.code,retryable=excluded.retryable,retry_after=excluded.retry_after,
                result_id=NULL,updated_at=excluded.updated_at""",
                (task_id, input_key, attempts, code, int(retry), retry_after, now))
        return {"attempts": attempts, "code": code, "retry_after": retry_after}

    def task_failures(self, *, limit=100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("INVALID_STATUS_LIMIT")
        return [dict(row) for row in self.connection.execute("""SELECT task.task_id,
            COALESCE(delivery.last_error,task.code) AS code,
            COALESCE(delivery.attempts,task.attempts) AS attempts,
            CASE WHEN delivery.status='RETRY' THEN delivery.next_attempt ELSE task.retry_after END AS retry_after,
            task.result_id,delivery.status AS delivery_status,task.updated_at
            FROM worker_tasks AS task LEFT JOIN sender_outbox AS delivery ON delivery.result_id=task.result_id
            WHERE task.code IS NOT NULL OR delivery.status IN ('RETRY','REJECTED','DEAD')
            ORDER BY task.updated_at DESC,task.task_id LIMIT ?""", (limit,))]

    def reset_task_failure(self, task_id: str):
        with self._transaction():
            removed = self.connection.execute("""DELETE FROM worker_tasks WHERE task_id=? AND (
                code IS NOT NULL OR result_id IN (
                    SELECT result_id FROM sender_outbox WHERE status IN ('REJECTED','DEAD')
                ))""", (task_id,)).rowcount
            if removed != 1:
                raise ValidationError("TASK_FAILURE_NOT_RESETTABLE")

    def _record(self, result_id, state, attempt, now, code=None, receipt_id=None):
        self.connection.execute("""INSERT INTO sender_attempts
            (result_id,state,attempt,code,receipt_id,occurred_at) VALUES (?,?,?,?,?,?)""",
            (result_id, state, attempt, code, receipt_id, now))

    def claim(self, *, now: float, lease_seconds: float = 180, max_attempts: int = 8) -> Claim | None:
        _time(now)
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (float, int))
                or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600
                or isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 100):
            raise ValidationError("INVALID_CLAIM_BOUNDS")
        with self._transaction():
            self.connection.execute("""INSERT INTO sender_attempts
                (result_id,state,attempt,code,occurred_at)
                SELECT result_id,CASE WHEN attempts>=? THEN 'DEAD' ELSE 'RETRY' END,
                attempts,'LOCAL_LEASE_EXPIRED',? FROM sender_outbox
                WHERE status='SENDING' AND lease_expires<=?""", (max_attempts, now, now))
            self.connection.execute("""UPDATE sender_outbox SET
                status=CASE WHEN attempts>=? THEN 'DEAD' ELSE 'RETRY' END,
                next_attempt=?, local_lease=NULL, lease_expires=NULL,
                last_error='LOCAL_LEASE_EXPIRED',updated_at=?
                WHERE status='SENDING' AND lease_expires<=?""", (max_attempts, now, now, now))
            self.connection.execute("""UPDATE sender_outbox SET status='DEAD',updated_at=?
                WHERE status IN ('PENDING','RETRY') AND attempts>=?""", (now, max_attempts))
            row = self.connection.execute("""SELECT * FROM sender_outbox
                WHERE status IN ('PENDING','RETRY') AND next_attempt<=? AND attempts<?
                ORDER BY created_at,result_id LIMIT 1""", (now, max_attempts)).fetchone()
            if row is None:
                return None
            local_lease = uuid.uuid4().hex
            self.connection.execute("""UPDATE sender_outbox SET status='SENDING',attempts=attempts+1,
                local_lease=?,lease_expires=?,updated_at=? WHERE result_id=?""",
                (local_lease, now + lease_seconds, now, row["result_id"]))
            self._record(row["result_id"], "SENDING", row["attempts"] + 1, now)
            return Claim(row["result_id"], row["payload_sha256"], bytes(row["body"]),
                         local_lease, row["attempts"] + 1)

    def finish(self, claim: Claim, *, now: float, receipt_id: str | None = None,
               error_code: str | None = None, retry: bool = False, max_attempts: int = 8):
        _time(now)
        with self._transaction():
            row = self.connection.execute("""SELECT attempts FROM sender_outbox
                WHERE result_id=? AND status='SENDING' AND local_lease=? AND lease_expires>?""",
                (claim.result_id, claim.lease, now)).fetchone()
            if row is None:
                raise LeaseLost("LOCAL_LEASE_LOST")
            if receipt_id is not None:
                status, next_attempt, error_code = "RECEIVED", now, None
            else:
                status = ("DEAD" if row["attempts"] >= max_attempts else "RETRY") if retry else "REJECTED"
                next_attempt = now + min(300, 2 ** min(row["attempts"], 8)) if retry else now
            self.connection.execute("""UPDATE sender_outbox SET status=?,next_attempt=?,
                local_lease=NULL,lease_expires=NULL,last_error=?,receipt_id=?,updated_at=?
                WHERE result_id=?""", (status, next_attempt, error_code, receipt_id, now, claim.result_id))
            self._record(claim.result_id, status, row["attempts"], now, error_code, receipt_id)

    def status(self, *, limit=100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("INVALID_STATUS_LIMIT")
        # Never return the candidate payload, remote lease token or credentials to logs/CLI.
        return [dict(row) for row in self.connection.execute("""SELECT result_id,status,attempts,
            next_attempt,last_error,receipt_id,created_at,updated_at
            FROM sender_outbox ORDER BY created_at,result_id LIMIT ?""", (limit,))]

    def result_status(self, result_id: str) -> str:
        row = self.connection.execute("SELECT status FROM sender_outbox WHERE result_id=?",
                                      (result_id,)).fetchone()
        if row is None:
            raise ValidationError("OUTBOX_RESULT_NOT_FOUND")
        return row["status"]

    def requeue(self, result_id: str, *, now: float):
        _time(now)
        with self._transaction():
            row = self.connection.execute("SELECT status FROM sender_outbox WHERE result_id=?", (result_id,)).fetchone()
            if row is None or row["status"] not in {"DEAD", "REJECTED"}:
                raise ValidationError("RESULT_NOT_REQUEUEABLE")
            self.connection.execute("""UPDATE sender_outbox SET status='PENDING',attempts=0,
                next_attempt=?,last_error=NULL,updated_at=? WHERE result_id=?""", (now, now, result_id))
            self._record(result_id, "REQUEUED", 0, now)

    def history(self, result_id: str, *, limit=100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("INVALID_STATUS_LIMIT")
        return [dict(row) for row in self.connection.execute("""SELECT event_id,result_id,state,
            attempt,code,receipt_id,occurred_at FROM sender_attempts
            WHERE result_id=? ORDER BY event_id DESC LIMIT ?""", (result_id, limit))]

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
