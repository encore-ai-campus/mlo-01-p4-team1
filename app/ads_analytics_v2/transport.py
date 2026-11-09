import json
import time

from .errors import AnalyticsError, LeaseLost
from .events import Event, canonical


class MemoryBroker:
    """Deliberately permits duplicate delivery, like an at-least-once transport."""

    def __init__(self):
        self.messages = []

    def publish(self, *, key, value):
        self.messages.append((key, value))


def publish_once(store, broker, *, clock=time.time, limit=100, lease_seconds=60, max_attempts=8):
    if not 1 <= limit <= 10_000 or lease_seconds <= 0 or max_attempts < 1:
        raise AnalyticsError("INVALID_PUBLISH_BOUNDS")
    counts = {"claimed": 0, "sent": 0, "failed": 0, "lease_lost": 0}
    for _ in range(limit):
        claim = store.claim(now=clock(), lease_seconds=lease_seconds, max_attempts=max_attempts)
        if claim is None:
            break
        counts["claimed"] += 1
        try:
            broker.publish(
                key=claim.event.delivery_id.encode("utf-8"),
                value=canonical(claim.event.envelope()).encode("utf-8"),
            )
        except Exception:  # noqa: BLE001 - An arbitrary broker failure must release the durable lease.
            counts["failed"] += 1
            try:
                store.release(claim, now=clock(), delay=min(300, 2 ** min(claim.attempt, 8)))
            except LeaseLost:
                counts["lease_lost"] += 1
            continue
        # Broker ACK followed by a crash before this commit may cause a replay, not loss.
        try:
            store.acknowledge(claim, now=clock())
            counts["sent"] += 1
        except LeaseLost:
            counts["lease_lost"] += 1
    return counts


def consume_messages(store, messages):
    events = []
    try:
        for key, payload in messages:
            event = Event.from_envelope(json.loads(payload))
            if key != event.delivery_id.encode("utf-8"):
                raise AnalyticsError("KAFKA_KEY_MISMATCH")
            events.append(event)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        raise AnalyticsError("INVALID_KAFKA_MESSAGE") from None
    return store.ingest(events, enqueue=False)
