"""Opt-in transactional V2 collections. The V1 source is never modified."""

import time

from ..errors import AnalyticsError, CapacityExceeded, Conflict, LeaseLost
from ..events import Event, canonical, stamp, timestamp
from ..sqlite_store import Claim, validate_claim, validate_requeue

COLLECTIONS = {
    "events": "ads_analytics_v2_events",
    "outbox": "ads_analytics_v2_outbox",
    "reports": "ads_analytics_v2_reports",
    "captures": "ads_analytics_v2_captures",
    "requeues": "ads_analytics_v2_requeues",
}


class MongoStore:
    def __init__(self, database):
        self.database = database
        self.events = database[COLLECTIONS["events"]]
        self.outbox = database[COLLECTIONS["outbox"]]
        self.reports = database[COLLECTIONS["reports"]]
        self.captures = database[COLLECTIONS["captures"]]
        self.requeues = database[COLLECTIONS["requeues"]]
        # Separate databases are required even if callers use distinct MongoClients.
        self.storage_identity = ("mongo-database", database.name)

    def prepare_indexes(self):
        self.outbox.create_index([("state", 1), ("due_at", 1), ("lease_until", 1)])
        self.reports.create_index([("report_as_of_key", -1), ("saved_at_ns", -1)])
        return dict(COLLECTIONS)

    def _ingest(self, events, *, enqueue, now, session):
        added = 0
        for event in events:
            old = self.events.find_one({"_id": event.event_id}, session=session)
            if old and old["fingerprint"] != event.fingerprint:
                raise Conflict("EVENT_ID_CONFLICT")
            if not old:
                self.events.insert_one(
                    {
                        "_id": event.event_id,
                        "fingerprint": event.fingerprint,
                        "body": event.envelope(),
                    },
                    session=session,
                )
                added += 1
            if enqueue:
                self.outbox.update_one(
                    {"_id": event.event_id},
                    {
                        "$setOnInsert": {
                            "state": "PENDING",
                            "due_at": now,
                            "attempts": 0,
                            "lease_token": None,
                            "lease_until": None,
                            "error_code": None,
                            "body": event.envelope(),
                        }
                    },
                    upsert=True,
                    session=session,
                )
        return added

    def _transaction(self, operation):
        from pymongo.errors import DuplicateKeyError

        for attempt in range(3):
            try:
                with self.database.client.start_session() as session:
                    return session.with_transaction(operation)
            except DuplicateKeyError:
                if attempt == 2:
                    raise Conflict("CONCURRENT_INGEST_RETRY_REQUIRED") from None

    def ingest(self, events, *, enqueue=False, now=0.0):
        events = list(events)
        return self._transaction(
            lambda session: self._ingest(events, enqueue=enqueue, now=now, session=session)
        )

    def capture_state(self, source_id):
        row = self.captures.find_one({"_id": source_id})
        return {**row["body"], "version": row["version"]} if row else None

    def ingest_capture(self, events, *, source_id, expected_version, state, now=0):
        events = list(events)

        def operation(session):
            old = self.captures.find_one({"_id": source_id}, session=session)
            version = old["version"] if old else 0
            if version != expected_version:
                raise Conflict("CAPTURE_STATE_CHANGED")
            added = self._ingest(events, enqueue=True, now=now, session=session)
            self.captures.replace_one(
                {"_id": source_id},
                {"_id": source_id, "version": version + 1, "body": state},
                upsert=True,
                session=session,
            )
            return added

        return self._transaction(operation)

    def iter_events(self, *, batch_size=1000):
        if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
            raise AnalyticsError("INVALID_EVENT_BATCH_SIZE")
        cursor = self.events.find({}, {"body": 1}).sort("_id", 1).batch_size(batch_size)
        try:
            for row in cursor:
                yield Event.from_envelope(row["body"])
        finally:
            cursor.close()

    def read_events(self, *, limit=100_000):
        rows = list(self.events.find({}, {"body": 1}).sort("_id", 1).limit(limit + 1))
        if len(rows) > limit:
            raise CapacityExceeded("REPORT_EVENT_LIMIT")
        return [Event.from_envelope(row["body"]) for row in rows]

    def claim(self, *, now, lease_seconds=60, max_attempts=8):
        validate_claim(now, lease_seconds, max_attempts)
        from uuid import uuid4

        from pymongo import ReturnDocument

        due = {
            "$or": [
                {"state": "PENDING", "due_at": {"$lte": now}},
                {"state": "SENDING", "lease_until": {"$lte": now}},
            ]
        }
        self.outbox.update_many(
            {**due, "attempts": {"$gte": max_attempts}}, {"$set": {"state": "DEAD"}}
        )
        token = uuid4().hex
        row = self.outbox.find_one_and_update(
            {**due, "attempts": {"$lt": max_attempts}},
            {
                "$set": {
                    "state": "SENDING",
                    "lease_token": token,
                    "lease_until": now + lease_seconds,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("due_at", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )
        return Claim(Event.from_envelope(row["body"]), token, row["attempts"]) if row else None

    def _finish(self, claim, now, *, success, delay=0, code=None):
        outcome = self.outbox.update_one(
            {
                "_id": claim.event.event_id,
                "state": "SENDING",
                "lease_token": claim.lease_token,
                "lease_until": {"$gt": now},
            },
            {
                "$set": {
                    "state": "SENT" if success else "PENDING",
                    "due_at": now + delay,
                    "lease_token": None,
                    "lease_until": None,
                    "error_code": code,
                }
            },
        )
        if outcome.modified_count != 1:
            raise LeaseLost("OUTBOX_LEASE_LOST")

    def acknowledge(self, claim, *, now):
        self._finish(claim, now, success=True)

    def release(self, claim, *, now, delay, code="BROKER_SEND_FAILED"):
        self._finish(claim, now, success=False, delay=delay, code=code)

    def queue_counts(self):
        return {
            row["_id"]: row["count"]
            for row in self.outbox.aggregate([{"$group": {"_id": "$state", "count": {"$sum": 1}}}])
        }

    def dead_letters(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AnalyticsError("INVALID_DEAD_LETTER_LIMIT")
        return [
            {
                "event_id": row["_id"],
                "attempts": row["attempts"],
                "error_code": row.get("error_code"),
            }
            for row in self.outbox.find({"state": "DEAD"}, {"attempts": 1, "error_code": 1})
            .sort("_id", 1)
            .limit(limit)
        ]

    def requeue_dead(self, event_ids, *, now):
        from uuid import uuid4

        values = validate_requeue(event_ids, now)

        def operation(session):
            rows = [self.outbox.find_one({"_id": identity}, session=session) for identity in values]
            if any(row is None or row["state"] != "DEAD" for row in rows):
                raise Conflict("REQUEUE_REQUIRES_DEAD")
            for row in rows:
                self.requeues.insert_one(
                    {
                        "_id": uuid4().hex,
                        "event_id": row["_id"],
                        "previous_attempts": row["attempts"],
                        "requeued_at": now,
                    },
                    session=session,
                )
                self.outbox.update_one(
                    {"_id": row["_id"]},
                    {
                        "$set": {
                            "state": "PENDING",
                            "due_at": now,
                            "attempts": 0,
                            "lease_token": None,
                            "lease_until": None,
                            "error_code": None,
                        }
                    },
                    session=session,
                )
            return len(values)

        return self._transaction(operation)

    def save_report(self, report):
        from pymongo.errors import DuplicateKeyError

        try:
            self.reports.insert_one(
                {
                    "_id": report["report_id"],
                    "body": report,
                    "saved_at_ns": time.time_ns(),
                    "report_as_of": timestamp(report["as_of"]),
                    "report_as_of_key": stamp(report["as_of"]),
                }
            )
        except DuplicateKeyError:
            old = self.reports.find_one({"_id": report["report_id"]})
            if old is None or canonical(old["body"]) != canonical(report):
                raise Conflict("REPORT_ID_CONFLICT") from None

    def latest_report(self, cutoff=None):
        # BSON Date is millisecond precision; the canonical UTC key preserves all
        # six fractional digits. Do not trust legacy BSON metadata for cutoffs.
        bound = stamp(cutoff) if cutoff is not None else "9999-12-31T23:59:59.999999Z"
        row = self.reports.find_one(
            {"body.scope": {}, "report_as_of_key": {"$lte": bound}},
            sort=[("report_as_of_key", -1), ("saved_at_ns", -1), ("_id", -1)],
        )

        def position(item):
            return (stamp(item["body"]["as_of"]), item.get("saved_at_ns", 0), item["_id"])

        # Read old documents without rewriting their immutable body/hash. This
        # compatibility scan keeps one candidate in memory and closes its cursor.
        legacy = self.reports.find(
            {"body.scope": {}, "report_as_of_key": {"$exists": False}},
            {"body": 1, "saved_at_ns": 1},
        ).batch_size(1000)
        try:
            for candidate in legacy:
                if position(candidate)[0] <= bound and (
                    row is None or position(candidate) > position(row)
                ):
                    row = candidate
        finally:
            legacy.close()
        return row["body"] if row else None

    def close(self):
        # The caller owns the MongoClient lifecycle.
        pass
