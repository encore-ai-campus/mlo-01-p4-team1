"""Explicit source -> durable outbox -> broker -> distinct sink -> materialization.

Clients and source permissions belong to the caller. No connection, thread, topic
creation or source mutation happens on import. Consumers must acknowledge/commit
their Kafka offsets only after consume_and_materialize returns successfully.
"""

import time
from itertools import islice

from .adapters.source_snapshot import capture_v1_page
from .errors import AnalyticsError, CapacityExceeded
from .reporting import report
from .transport import consume_messages, publish_once


def require_distinct_stores(producer_store, sink_store):
    if producer_store is sink_store or (
        getattr(producer_store, "storage_identity", None) is not None
        and producer_store.storage_identity == getattr(sink_store, "storage_identity", None)
    ):
        raise AnalyticsError("PRODUCER_AND_SINK_MUST_BE_DISTINCT")


def consume_and_materialize(
    sink_store,
    messages,
    *,
    cutoff,
    aggregate=None,
    max_messages=10_000,
    max_events=100_000,
    settled_through=None,
):
    """Replay-safe sink ingestion, then a full immutable report.

    Failed aggregation leaves ingested events durable but no new report: a Kafka
    batch replay retries it. It never copies the producer's local event table.
    """
    if (
        type(max_messages) is not int
        or not 1 <= max_messages <= 10_000
        or type(max_events) is not int
        or not 1 <= max_events <= 1_000_000
    ):
        raise AnalyticsError("INVALID_PIPELINE_BOUNDS")
    received = list(islice(iter(messages), max_messages + 1))
    if len(received) > max_messages:
        raise CapacityExceeded("STREAM_BATCH_LIMIT")
    inserted = consume_messages(sink_store, received)
    aggregate = aggregate or report
    result = aggregate(
        sink_store.read_events(limit=max_events),
        cutoff=cutoff,
        settled_through=settled_through,
        max_events=max_events,
    )
    if result.get("scope") != {}:
        raise AnalyticsError("MATERIALIZED_REPORT_MUST_HAVE_FULL_SCOPE")
    sink_store.save_report(result)
    return {"received": len(received), "inserted": inserted, "report": result}


def run_once(
    source_collection,
    producer_store,
    broker,
    sink_store,
    *,
    source_id,
    media_id,
    cutoff,
    messages=None,
    consumer=None,
    clock=time.time,
    page_size=1000,
    publish_limit=100,
    aggregate=None,
    max_events=100_000,
):
    """One collection page and publish pass; optionally consume actual broker input.

    The consumer is a zero-argument bounded poll callable returning (key, value)
    pairs. It must not commit offsets itself. No supplied consumer/messages means
    no materialization, rather than pretending a producer ACK proves consumption.
    """
    require_distinct_stores(producer_store, sink_store)
    if messages is not None and consumer is not None:
        raise AnalyticsError("MULTIPLE_PIPELINE_CONSUMERS")
    captured = capture_v1_page(
        source_collection,
        producer_store,
        source_id=source_id,
        media_id=media_id,
        cutoff=cutoff,
        page_size=page_size,
        now=clock(),
    )
    published = publish_once(producer_store, broker, clock=clock, limit=publish_limit)
    consumed = None
    if messages is not None or consumer is not None:
        incoming = messages if messages is not None else consumer()
        consumed = consume_and_materialize(
            sink_store, incoming, cutoff=cutoff, aggregate=aggregate, max_events=max_events
        )
    return {
        "capture": captured,
        "publish": published,
        "consume": consumed,
        "queue": producer_store.queue_counts(),
        "source_completeness_verified": False,
    }
