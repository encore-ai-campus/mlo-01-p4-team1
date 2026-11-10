"""Reproducible offline throughput and crash/replay evidence; not a cluster benchmark."""

import json
import math
import platform
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .audit import reconcile
from .errors import AnalyticsError
from .events import Event
from .reporting import report
from .sqlite_store import SQLiteStore
from .transport import MemoryBroker, consume_messages, publish_once


def _percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def synthetic_events(pairs):
    start = datetime(2026, 11, 6, tzinfo=UTC)
    events = []
    for index in range(pairs):
        occurred = start + timedelta(milliseconds=index * 2)
        common = {
            "delivery_id": f"synthetic-delivery-{index}",
            "ad_id": f"synthetic-ad-{index % 8}",
            "creative_version": "synthetic-1",
            "placement": "top" if index % 2 else "bottom",
            "media_id": "synthetic-media",
            "result": "COUNTED",
            "counted": True,
            "is_test": False,
            "source_policy_version": "synthetic-benchmark-only",
        }
        events.append(
            Event(
                event_id=f"synthetic-impression-{index}",
                event_type="VIEWABLE_IMPRESSION",
                occurred_at=occurred,
                received_at=occurred,
                **common,
            )
        )
        click = occurred + timedelta(milliseconds=1)
        events.append(
            Event(
                event_id=f"synthetic-click-{index}",
                event_type="CLICK",
                occurred_at=click,
                received_at=click,
                **common,
            )
        )
    return events


def run_benchmark(output, *, pairs=1000, batch_size=100):
    if type(pairs) is not int or not 1 <= pairs <= 10_000:
        raise AnalyticsError("BENCHMARK_PAIRS_OUT_OF_RANGE")
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise AnalyticsError("BENCHMARK_BATCH_OUT_OF_RANGE")
    output = Path(output)
    # Exclusive creation prevents overwriting earlier evidence or a live database.
    output.mkdir(parents=True, exist_ok=False)
    source_path, sink_path = output / "staged.sqlite3", output / "delivered.sqlite3"
    source, sink = SQLiteStore(source_path), SQLiteStore(sink_path)
    broker = MemoryBroker()
    events = synthetic_events(pairs)
    cutoff = datetime(2026, 11, 7, tzinfo=UTC)
    batch_latencies = []
    started = time.perf_counter()
    try:
        source.ingest(events, enqueue=True, now=1000)
        # Model process loss AFTER broker ACK but BEFORE the outbox commit.
        claim = source.claim(now=1000, lease_seconds=1)
        broker.publish(
            key=claim.event.delivery_id.encode(), value=json.dumps(claim.event.envelope()).encode()
        )
        source.close()
        source = SQLiteStore(source_path)
        while source.queue_counts().get("PENDING", 0) or source.queue_counts().get("SENDING", 0):
            before = time.perf_counter()
            result = publish_once(source, broker, clock=lambda: 1002, limit=batch_size)
            if result["failed"] or result["lease_lost"]:
                raise AnalyticsError("BENCHMARK_TRANSPORT_FAILED")
            batch_latencies.append((time.perf_counter() - before) * 1000)
        source.close()
        source = SQLiteStore(source_path)
        inserted = 0
        for offset in range(0, len(broker.messages), batch_size):
            inserted += consume_messages(sink, broker.messages[offset : offset + batch_size])
        sink.close()
        sink = SQLiteStore(sink_path)
        replay_inserted = consume_messages(sink, broker.messages)
        snapshot = report(sink.read_events(), cutoff=cutoff)
        sink.save_report(snapshot)
        comparison = reconcile(
            events,
            source.read_events(),
            sink.read_events(),
            snapshot,
            cutoff=cutoff,
            transport_events=[
                Event.from_envelope(json.loads(value)) for _, value in broker.messages
            ],
        )
        seconds = time.perf_counter() - started
        return {
            "mode": "OFFLINE_SQLITE_MEMORY_BROKER",
            "uses_real_kafka_mongodb_spark": False,
            "performance_scope": "synthetic ingest + publish + consume + replay + report + reconciliation",
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.system(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
            "input": {
                "synthetic_pairs": pairs,
                "events": len(events),
                "batch_size": batch_size,
                "injection": "unpaced",
                "input_rate_limit_events_per_second": None,
            },
            "elapsed_seconds": seconds,
            "unique_events_per_second": len(events) / seconds if seconds else None,
            "publish_batch_latency_ms": {
                "p50": _percentile(batch_latencies, 0.5),
                "p95": _percentile(batch_latencies, 0.95),
                "samples": len(batch_latencies),
            },
            "latency_scope": "publish batch wall time, not per-event end-to-end latency",
            "recovery": {
                "crash_after_ack_before_outbox_commit": True,
                "sqlite_reopened": True,
                "transport_messages": len(broker.messages),
                "initial_sink_inserted": inserted,
                "replay_inserted": replay_inserted,
                "queue": source.queue_counts(),
            },
            "totals": snapshot["totals"],
            "reconciliation": comparison,
        }
    finally:
        source.close()
        sink.close()
