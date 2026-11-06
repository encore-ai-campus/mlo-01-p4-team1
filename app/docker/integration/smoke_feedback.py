"""Local-only Django Outbox -> Kafka -> Django ingest/Projection check."""
import argparse
import json
import os
import uuid

def guard():
    if os.environ.get("MLO_LOCAL_INTEGRATION") != "1":
        raise RuntimeError("local_integration_not_enabled")
    if os.environ.get("MLO_LOCAL_DOCKER") != "1":
        raise RuntimeError("local_docker_not_enabled")
    if (os.environ.get("PGHOST"), os.environ.get("PGDATABASE")) != ("postgres", "muther_dev"):
        raise RuntimeError("refusing_nonlocal_postgres")
    if os.environ.get("FEEDBACK_KAFKA_TOPIC") != "feedback.v1.local":
        raise RuntimeError("refusing_nonlocal_kafka_topic")
    if not all(name in os.environ.get("FEEDBACK_KAFKA_BOOTSTRAP", "") for name in (
        "kafka-01:9092", "kafka-02:9092", "kafka-03:9092"
    )):
        raise RuntimeError("refusing_nonlocal_kafka")


def setup():
    import django
    django.setup()


def prepare():
    from django.db import transaction
    from backend.common.events import record
    identifier = str(uuid.uuid4())
    payload = dict(
        event_id=identifier, owner_id=str(uuid.uuid4()),
        family_id=str(uuid.uuid4()), request_id=str(uuid.uuid4()),
        source_scope="stage3_synthetic_feedback_local", is_test=True,
    )
    with transaction.atomic():
        record("RECOMMENDATION_REQUESTED", payload)
    print(json.dumps(dict(action="prepare", synthetic=True, event_id=identifier)))


def verify():
    from kafka import KafkaConsumer
    from backend.common.models import FeedbackEvent, Outbox, StreamRecord, Projection
    from pipeline.feedback.process import apply_record

    event = (FeedbackEvent.objects
             .filter(is_test=True, payload__source_scope="stage3_synthetic_feedback_local")
             .order_by("-occurred_at").first())
    if not event:
        raise RuntimeError("no_synthetic_feedback_event")
    outbox = Outbox.objects.get(pk=event.pk)
    if outbox.state != "SENT":
        raise RuntimeError("outbox_not_acknowledged")
    consumer = KafkaConsumer(
        os.environ["FEEDBACK_KAFKA_TOPIC"],
        bootstrap_servers=os.environ["FEEDBACK_KAFKA_BOOTSTRAP"],
        group_id=f"mlo-stage3-smoke-{uuid.uuid4()}",
        auto_offset_reset="earliest", consumer_timeout_ms=14000,
        enable_auto_commit=False,
        value_deserializer=lambda raw: json.loads(raw.decode("utf-8")),
    )
    processed = None
    stream_key = None
    try:
        for msg in consumer:
            if msg.value.get("event_id") != event.pk:
                continue
            stream_key = f"{msg.topic}:{msg.partition}:{msg.offset}"
            processed = apply_record(msg.value, msg.topic, msg.partition, msg.offset)
            again = apply_record(msg.value, msg.topic, msg.partition, msg.offset)
            if again["projected"] or again["raw_created"]:
                raise RuntimeError("feedback_idempotency_failed")
            break
    finally:
        consumer.close()
    if not processed or not processed["projected"] or not processed["raw_created"]:
        raise RuntimeError("feedback_not_projected")
    if not StreamRecord.objects.filter(pk=stream_key, event_id=event.pk).exists():
        raise RuntimeError("raw_offset_not_stored")
    if not Projection.objects.filter(event=event).exists():
        raise RuntimeError("feedback_projection_missing")
    print(json.dumps(dict(
        result="PASS", event_type=event.event_type,
        source="Django Outbox -> Kafka RF3 -> original feedback ingest",
        outbox=outbox.state, raw_offset_saved=True, projected=True,
        duplicate_applied=False, is_test=True,
    )))


def verify_spark(event_id):
    from backend.common.models import FeedbackEvent, Outbox, StreamRecord, Projection
    event = FeedbackEvent.objects.get(pk=event_id)
    if not (event.is_test and event.payload.get("source_scope") == "stage3_synthetic_feedback_local"):
        raise RuntimeError("refusing_nonlocal_or_unknown_feedback_fixture")
    outbox = Outbox.objects.get(pk=event.pk)
    if outbox.state != "SENT":
        raise RuntimeError("feedback_outbox_not_sent")
    records = list(StreamRecord.objects.filter(event_id=event.pk))
    if len(records) != 1 or not records[0].stream_key.startswith("feedback.v1.local:"):
        raise RuntimeError("feedback_kafka_offset_not_ingested")
    if not Projection.objects.filter(event=event).exists():
        raise RuntimeError("feedback_spark_projection_not_created")
    print(json.dumps({
        "result": "PASS", "path": "Django Outbox -> Kafka -> original Spark feedback Job -> PostgreSQL",
        "synthetic": True, "event_id": event.pk,
        "outbox_state": outbox.state, "stream_records": len(records), "projected": True,
    }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "verify", "verify-spark"])
    parser.add_argument("--event-id", default="")
    args = parser.parse_args()
    guard()
    setup()
    if args.command == "prepare":
        prepare()
    elif args.command == "verify":
        verify()
    else:
        if not args.event_id:
            raise RuntimeError("explicit_test_event_id_required")
        verify_spark(args.event_id)


if __name__ == "__main__":
    main()
