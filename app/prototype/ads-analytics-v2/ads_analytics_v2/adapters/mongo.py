"""Opt-in storage in new collections on an existing MongoDB replica set.

Construct with an explicitly supplied database. The V1 collections are never used.
Index preparation is a separate explicit call; this module does not provision users.
"""
from ..errors import CapacityExceeded, Conflict, LeaseLost
from ..events import Event, canonical
from ..sqlite_store import Claim, validate_claim

COLLECTIONS = {
    "events": "ads_analytics_v2_events",
    "outbox": "ads_analytics_v2_outbox",
    "reports": "ads_analytics_v2_reports",
}


class MongoStore:
    def __init__(self, database):
        self.database = database
        self.events = database[COLLECTIONS["events"]]
        self.outbox = database[COLLECTIONS["outbox"]]
        self.reports = database[COLLECTIONS["reports"]]

    def prepare_indexes(self):
        # Call only after review/authorization against the intended database.
        self.outbox.create_index([("state", 1), ("due_at", 1), ("lease_until", 1)])
        return dict(COLLECTIONS)

    def ingest(self, events, *, enqueue=False, now=0.0):
        from pymongo.errors import DuplicateKeyError
        events = list(events)

        def operation(session):
            added = 0
            for event in events:
                old = self.events.find_one({"_id": event.event_id}, session=session)
                if old and old["fingerprint"] != event.fingerprint:
                    raise Conflict("EVENT_ID_CONFLICT")
                if not old:
                    self.events.insert_one({"_id": event.event_id, "fingerprint": event.fingerprint,
                                            "body": event.envelope()}, session=session)
                    added += 1
                if enqueue:
                    self.outbox.update_one({"_id": event.event_id}, {"$setOnInsert": {
                        "state": "PENDING", "due_at": now, "attempts": 0,
                        "lease_token": None, "lease_until": None, "error_code": None,
                        "body": event.envelope(),
                    }}, upsert=True, session=session)
            return added

        # A concurrent identical insert can abort a transaction; retry with a fresh snapshot.
        for attempt in range(3):
            try:
                with self.database.client.start_session() as session:
                    return session.with_transaction(operation)
            except DuplicateKeyError:
                if attempt == 2:
                    raise Conflict("CONCURRENT_INGEST_RETRY_REQUIRED") from None

    def read_events(self, *, limit=100_000):
        rows = list(self.events.find({}, {"body": 1}).sort("_id", 1).limit(limit + 1))
        if len(rows) > limit:
            raise CapacityExceeded("REPORT_EVENT_LIMIT")
        return [Event.from_envelope(row["body"]) for row in rows]

    def claim(self, *, now, lease_seconds=60, max_attempts=8):
        validate_claim(now, lease_seconds, max_attempts)
        from uuid import uuid4
        from pymongo import ReturnDocument
        due = {"$or": [{"state": "PENDING", "due_at": {"$lte": now}},
                       {"state": "SENDING", "lease_until": {"$lte": now}}]}
        self.outbox.update_many({**due, "attempts": {"$gte": max_attempts}},
                                {"$set": {"state": "DEAD"}})
        token = uuid4().hex
        row = self.outbox.find_one_and_update(
            {**due, "attempts": {"$lt": max_attempts}},
            {"$set": {"state": "SENDING", "lease_token": token,
                      "lease_until": now + lease_seconds}, "$inc": {"attempts": 1}},
            sort=[("due_at", 1), ("_id", 1)], return_document=ReturnDocument.AFTER)
        return Claim(Event.from_envelope(row["body"]), token, row["attempts"]) if row else None

    def _finish(self, claim, now, *, success, delay=0, code=None):
        outcome = self.outbox.update_one(
            {"_id": claim.event.event_id, "state": "SENDING", "lease_token": claim.lease_token,
             "lease_until": {"$gt": now}},
            {"$set": {"state": "SENT" if success else "PENDING", "due_at": now + delay,
                      "lease_token": None, "lease_until": None, "error_code": code}})
        if outcome.modified_count != 1:
            raise LeaseLost("OUTBOX_LEASE_LOST")

    def acknowledge(self, claim, *, now):
        self._finish(claim, now, success=True)

    def release(self, claim, *, now, delay, code="BROKER_SEND_FAILED"):
        self._finish(claim, now, success=False, delay=delay, code=code)

    def queue_counts(self):
        return {row["_id"]: row["count"] for row in self.outbox.aggregate([
            {"$group": {"_id": "$state", "count": {"$sum": 1}}}])}

    def save_report(self, report):
        from pymongo.errors import DuplicateKeyError
        try:
            self.reports.insert_one({"_id": report["report_id"], "body": report})
        except DuplicateKeyError:
            old = self.reports.find_one({"_id": report["report_id"]})
            if old is None or canonical(old["body"]) != canonical(report):
                raise Conflict("REPORT_ID_CONFLICT") from None

    def close(self):
        # The caller owns the MongoClient lifecycle.
        pass
