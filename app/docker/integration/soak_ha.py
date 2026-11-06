"""Bounded local-only continuous Kafka -> Spark HA -> ES verification.

Requires --local-only guard environment. The Docker service has a dedicated topic,
index, and checkpoint to avoid interference with the stage 3 weather stream.
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.weather.common import KST, encode
from smoke_stream import event, mapping_grid

TOPIC = "weather.ha.soak.v1.local"
INDEX = "weather-ha-soak-v1-local"
ES_URL = "http://es-router:9200"
BROKERS = "kafka-01:9092,kafka-02:9092,kafka-03:9092"
CHECKPOINT = Path("/opt/mlo/checkpoints/weather-ha-soak")


def guard():
    expected = dict(
        MLO_LOCAL_INTEGRATION="1", KAFKA_TOPIC=TOPIC,
        WEATHER_INDEX=INDEX, ES_URL=ES_URL, KAFKA_BOOTSTRAP=BROKERS,
    )
    for key, value in expected.items():
        if os.getenv(key) != value:
            raise RuntimeError(f"local-only guard rejected {key}")


def kafka_client():
    from kafka.admin import KafkaAdminClient
    return KafkaAdminClient(bootstrap_servers=BROKERS, request_timeout_ms=15000)


def partitions():
    from kafka import KafkaConsumer, TopicPartition
    consumer = KafkaConsumer(bootstrap_servers=BROKERS, request_timeout_ms=15000)
    try:
        parts = consumer.partitions_for_topic(TOPIC)
        if parts != {0, 1, 2}:
            raise RuntimeError(f"topic partition contract mismatch: {parts}")
        return {str(k.partition): v for k, v in consumer.end_offsets(
            [TopicPartition(TOPIC, part) for part in sorted(parts)]
        ).items()}
    finally:
        consumer.close()


def prepare():
    from kafka.admin import NewTopic
    from pipeline.weather.prepare_es import ensure_index
    admin = kafka_client()
    try:
        if TOPIC in admin.list_topics():
            raise RuntimeError("refusing existing soak topic: use a fresh fixture")
        admin.create_topics([
            NewTopic(name=TOPIC, num_partitions=3, replication_factor=3,
                     topic_configs={"min.insync.replicas": "2"})
        ])
    finally:
        admin.close()
    ensure_index()
    print(json.dumps({"result": "PREPARED", "topic": TOPIC,
                      "index": INDEX, "end_offsets": partitions()}), flush=True)


def publish(run_id, count, interval):
    from kafka import KafkaProducer
    if not (1 <= count <= 500 and 0.1 <= interval <= 5.0):
        raise RuntimeError("bounded count/interval required")
    if not run_id or len(run_id) > 32 or not all(x.isalnum() or x == "-" for x in run_id):
        raise RuntimeError("unsafe run id")
    ver, grid = mapping_grid()
    start = datetime.now(KST)
    issued_base = start - timedelta(minutes=5)
    obs_time = start.replace(minute=0, second=0, microsecond=0)
    producer = KafkaProducer(
        bootstrap_servers=BROKERS, acks="all", retries=8,
        max_block_ms=15000, value_serializer=encode,
    )
    accepted = 0
    first_offset = last_offset = partition = None
    try:
        for seq in range(count):
            at = datetime.now(KST).isoformat()
            issued = (issued_base + timedelta(milliseconds=seq * 137)).isoformat()
            payload = event(ver, grid, at, "odam", "observation", "T1H",
                            issued, obs_time.isoformat(), 20.0 + (seq % 20) / 10, seq)
            payload["run_id"] = f"mlo-local-soak-{run_id}"
            payload["request_id"] = f"mlo-ha-soak-{run_id}-{seq:04d}"
            record = producer.send(TOPIC, key=b"58_125", value=payload).get(timeout=20)
            partition = record.partition
            if first_offset is None:
                first_offset = record.offset
            last_offset = record.offset
            accepted += 1
            if accepted % 15 == 0:
                print(json.dumps({"phase": "PRODUCING", "accepted": accepted,
                                  "partition": partition, "last_offset": last_offset}), flush=True)
            time.sleep(interval)
        producer.flush(timeout=20)
    finally:
        producer.close(timeout=10)
    print(json.dumps({
        "phase": "PUBLISHED", "run_id": run_id, "topic": TOPIC, "count": accepted,
        "first_offset": first_offset, "last_offset": last_offset,
        "partition": partition, "end_offsets": partitions(),
        "last_request_id": f"mlo-ha-soak-{run_id}-{count-1:04d}",
    }), flush=True)


def committed_offsets():
    commits = CHECKPOINT / "commits"
    offsets = CHECKPOINT / "offsets"
    if not commits.is_dir() or not offsets.is_dir():
        raise RuntimeError("missing_soak_checkpoint")
    batch_numbers = sorted(int(p.name) for p in commits.iterdir() if p.name.isdecimal())
    if not batch_numbers:
        raise RuntimeError("missing_committed_spark_batches")
    latest = batch_numbers[-1]
    lines = (offsets / str(latest)).read_text(encoding="utf-8").splitlines()
    if len(lines) < 3 or lines[0] != "v1":
        raise RuntimeError("unrecognized_spark_checkpoint_format")
    data = json.loads(lines[2])
    if TOPIC not in data:
        raise RuntimeError("wrong_topic_in_checkpoint")
    return latest, {str(k): int(v) for k, v in data[TOPIC].items()}


def verify(run_id, count):
    from pipeline.weather.es_current import request
    from kafka.admin import KafkaAdminClient
    ends = partitions()
    batch, done = committed_offsets()
    if sum(ends.values()) != count:
        raise RuntimeError(f"unexpected_kafka_message_count expected={count} got={sum(ends.values())}")
    if ends != done:
        raise RuntimeError(f"not_caught_up kafka={ends} spark_committed={done}")
    result = request(ES_URL, "GET", f"/{INDEX}/_doc/58_125")
    if not result.get("found"):
        raise RuntimeError("expected_ES_grid_not_found")
    rows = result["_source"].get("rows", [])
    marker = f"mlo-ha-soak-{run_id}-{count-1:04d}"
    expected = {f"mlo-ha-soak-{run_id}-{i:04d}" for i in range(count)}
    observed = [r.get("request_id") for r in rows]
    if len(observed) != count or len(set(observed)) != count or set(observed) != expected:
        raise RuntimeError(
            f"es_missing_or_duplicate_events rows={len(observed)} "
            f"unique={len(set(observed))} missing={len(expected-set(observed))}"
        )
    if result["_source"].get("revision", 0) < 1:
        raise RuntimeError("invalid_es_revision")
    print(json.dumps({
        "result": "PASS", "published": count,
        "spark_latest_committed_batch": batch,
        "kafka_end_offsets": ends, "spark_committed_offsets": done,
        "es_latest_request_id": marker, "es_revision": result["_source"]["revision"],
        "es_rows": len(rows),
    }), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "publish", "verify"])
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--count", type=int, default=180)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    guard()
    {"prepare": prepare, "publish": lambda: publish(args.run_id, args.count, args.interval),
     "verify": lambda: verify(args.run_id, args.count)}[args.action]()


if __name__ == "__main__":
    main()
