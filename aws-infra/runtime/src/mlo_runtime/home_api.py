"""FAKE HOME API FOR LOCAL TESTS ONLY: SQLite dedup and observable fake slots.

This is deliberately not a model implementation. Real model admission,
cancellation and GPU release must implement the same acceptance contract.
"""

import hashlib
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import sqlite3
import threading
import time

from .operations import UnsafeTransition
from .relay import envelope


class FakeHome:
    def __init__(self, database, delay=0, slots=1):
        self.database, self.delay, self.slots = str(database), delay, slots
        self.received = []
        self._threads = []
        self._cancel_events = {}
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, digest TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL, result TEXT, released INTEGER NOT NULL, submissions INTEGER NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=3)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _finish(self, request):
        self._cancel_events[request["request_id"]].wait(self.delay)
        result = {
            "request_id": request["request_id"],
            "revision": request["revision"],
            "candidate": request["candidates"][0],
        }
        with self._connect() as connection:
            # Only the fake worker releases its slot, after it has stopped work.
            connection.execute(
                "UPDATE requests SET state='cancelled',released=1 WHERE id=? AND state='requested'",
                (request["request_id"],),
            )
            connection.execute(
                "UPDATE requests SET state=?,result=?,released=1 WHERE id=? AND state='running'",
                (
                    "completed" if time.time() <= request["deadline"] else "expired",
                    json.dumps(result),
                    request["request_id"],
                ),
            )

    def submit(self, raw):
        request = envelope(raw)
        self.received.append(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT digest FROM requests WHERE id=?", (request["request_id"],)
            ).fetchone()
            if row:
                if row[0] != digest:
                    return 409, {"error": "request_id_body_conflict"}
            else:
                if request["deadline"] <= time.time():
                    return 408, {"error": "deadline"}
                busy = connection.execute(
                    "SELECT count(*) FROM requests WHERE released=0"
                ).fetchone()[0]
                if busy >= self.slots:
                    return 429, {"error": "no_fake_slot"}
                connection.execute(
                    "INSERT INTO requests VALUES(?,?,?,'running',NULL,0,1)",
                    (request["request_id"], digest, raw.decode()),
                )
        if not row:
            self._cancel_events[request["request_id"]] = threading.Event()
            worker = threading.Thread(target=self._finish, args=(request,), daemon=True)
            self._threads.append(worker)
            worker.start()
        return self.lookup(request["request_id"])

    def lookup(self, request_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state,result,released,submissions FROM requests WHERE id=?",
                (request_id,),
            ).fetchone()
        if not row:
            return 404, {"error": "unknown_id"}
        state, result, released, submissions = row
        return (200 if state not in {"running", "requested"} else 202), {
            **(json.loads(result) if result else {}),
            "request_id": request_id,
            "state": state,
            "gpu_slot_released": bool(released),
            "submissions": submissions,
            "fake_home": True,
        }

    def cancel(self, request_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE requests SET state='requested' WHERE id=? AND state='running'",
                (request_id,),
            )
        event = self._cancel_events.get(request_id)
        if event:
            event.set()
        return self.lookup(request_id)

    def status(self):
        with self._connect() as connection:
            active = connection.execute(
                "SELECT count(*) FROM requests WHERE released=0"
            ).fetchone()[0]
        return {
            "ready": True,
            "active_slots": active,
            "capacity": self.slots,
            "fake_home": True,
        }

    def close(self):
        for worker in self._threads:
            worker.join(timeout=self.delay + 1)


def fake_handler(home, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _handle(self):
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(401)
                return
            try:
                if self.command == "POST" and self.path == "/requests":
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        raise UnsafeTransition("body bounds")
                    raw = self.rfile.read(size)
                    if self.headers.get("X-Request-ID") != envelope(raw)["request_id"]:
                        raise UnsafeTransition("request id mismatch")
                    code, value = home.submit(raw)
                elif self.command == "GET" and self.path == "/status":
                    code, value = 200, home.status()
                elif self.path.startswith("/requests/") and self.command in {
                    "GET",
                    "DELETE",
                }:
                    operation = home.lookup if self.command == "GET" else home.cancel
                    code, value = operation(self.path.split("/")[-1])
                else:
                    code, value = 404, {"error": "unknown_path"}
            except (ValueError, TypeError, UnsafeTransition):
                code, value = 400, {"error": "invalid_envelope"}
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET = do_DELETE = _handle

    return Handler
