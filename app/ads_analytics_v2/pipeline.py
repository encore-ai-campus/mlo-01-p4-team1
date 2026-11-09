"""Explicit collection, durable transport and bounded-memory materialization."""
import time
from itertools import islice

from .adapters.source_snapshot import capture_v1_page
from .errors import AnalyticsError, CapacityExceeded
from .events import identifier
from .reporting import validate_query
from .sqlite_store import validate_claim
from .streaming_report import report_streaming, validate_stream_bounds
from .transport import consume_messages, publish_once


def require_distinct_stores(producer_store, sink_store):
    if producer_store is sink_store or (
        getattr(producer_store, "storage_identity", None) is not None
        and producer_store.storage_identity == getattr(sink_store, "storage_identity", None)
    ):
        raise AnalyticsError("PRODUCER_AND_SINK_MUST_BE_DISTINCT")


def _validate_materialization(cutoff, settled_through, aggregate, max_events, batch_size, work_dir):
    validate_query(cutoff, settled_through)
    validate_stream_bounds(max_events, batch_size, work_dir=work_dir)
    if aggregate is not None and not callable(aggregate):
        raise AnalyticsError("INVALID_REPORT_RENDERER")


def consume_and_materialize(sink_store, messages, *, cutoff, aggregate=None,
                            max_messages=10_000, max_events=None, settled_through=None,
                            batch_size=1000, work_dir=None):
    """Validate before writes; replay-safe ingestion precedes bounded full aggregation.

    A failed aggregate leaves events durable but no new report, so Kafka offsets
    must only be committed after this call succeeds. Storage iteration is batched;
    its total retained event count has no implicit 100,000-event ceiling.
    """
    _validate_materialization(cutoff, settled_through, aggregate, max_events, batch_size, work_dir)
    if type(max_messages) is not int or not 1 <= max_messages <= 10_000:
        raise AnalyticsError("INVALID_PIPELINE_BOUNDS")
    received = list(islice(iter(messages), max_messages + 1))
    if len(received) > max_messages:
        raise CapacityExceeded("STREAM_BATCH_LIMIT")
    inserted = consume_messages(sink_store, received)
    aggregate = aggregate or report_streaming
    result = aggregate(sink_store.iter_events(batch_size=batch_size), cutoff=cutoff,
                       settled_through=settled_through, max_events=max_events,
                       batch_size=batch_size, work_dir=work_dir)
    if result.get("scope") != {}:
        raise AnalyticsError("MATERIALIZED_REPORT_MUST_HAVE_FULL_SCOPE")
    sink_store.save_report(result)
    return {"received": len(received), "inserted": inserted, "report": result}


def run_once(source_collection, producer_store, broker, sink_store, *, source_id,
             media_id, cutoff, messages=None, consumer=None, clock=time.time,
             page_size=1000, publish_limit=100, aggregate=None, max_events=None,
             batch_size=1000, work_dir=None):
    """One validated collection/publish pass and optional real broker consumption."""
    require_distinct_stores(producer_store, sink_store)
    _validate_materialization(cutoff, None, aggregate, max_events, batch_size, work_dir)
    if (type(page_size) is not int or not 1 <= page_size <= 10_000
            or type(publish_limit) is not int or not 1 <= publish_limit <= 10_000
            or (consumer is not None and not callable(consumer)) or not callable(clock)):
        raise AnalyticsError("INVALID_PIPELINE_BOUNDS")
    if messages is not None and consumer is not None:
        raise AnalyticsError("MULTIPLE_PIPELINE_CONSUMERS")
    if messages is not None:
        iter(messages)
    identifier(media_id)
    now = clock()
    validate_claim(now, 60, 8)
    captured = capture_v1_page(source_collection, producer_store, source_id=source_id,
        media_id=media_id, cutoff=cutoff, page_size=page_size, now=now)
    published = publish_once(producer_store, broker, clock=clock, limit=publish_limit)
    consumed = None
    if messages is not None or consumer is not None:
        incoming = messages if messages is not None else consumer()
        consumed = consume_and_materialize(sink_store, incoming, cutoff=cutoff,
            aggregate=aggregate, max_events=max_events, batch_size=batch_size, work_dir=work_dir)
    return {"capture": captured, "publish": published, "consume": consumed,
            "queue": producer_store.queue_counts(), "source_completeness_verified": False}
