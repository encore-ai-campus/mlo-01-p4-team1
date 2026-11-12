import json
import os
import time
from typing import Optional

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from backend.venues.models.outbox import VenueOutbox
from backend.venues.streaming.observability import emit_observation


def _build_kafka_producer(bootstrap: str):
    from kafka import KafkaProducer
    from pipeline.kafka_config import python_options

    return KafkaProducer(
        bootstrap_servers=bootstrap,
        acks='all',
        retries=3,
        **python_options('VENUE_OUTBOX_KAFKA_'),
        value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode('utf-8'),
    )


def _sanitized_outbox_message(row: VenueOutbox) -> dict:
    return {
        'event_id': row.event_id,
        'event_type': row.event_type,
        'venue_id': row.venue_id,
        'request_id': row.request_id,
        'payload': row.payload if isinstance(row.payload, dict) else {},
        'occurred_at': row.occurred_at.isoformat() if row.occurred_at else None,
    }


def relay_pending_batch(
    *,
    batch_size: int = 50,
    mode: str = 'auto',
    max_attempts: int = 5,
    producer=None,
    topic: Optional[str] = None,
) -> dict:
    """Relay up to `batch_size` PENDING VenueOutbox rows to Kafka or mlo.pipeline observations."""
    bootstrap = os.environ.get('VENUE_OUTBOX_KAFKA_BOOTSTRAP', '').strip() or os.environ.get('VENUE_KAFKA_BOOTSTRAP', '').strip()
    resolved_topic = (
        topic
        or os.environ.get('VENUE_OUTBOX_KAFKA_TOPIC', '').strip()
        or os.environ.get('VENUE_KAFKA_TOPIC', '').strip()
    )
    use_kafka = mode == 'kafka' or (mode == 'auto' and bool(bootstrap and resolved_topic))

    if use_kafka and producer is None:
        if not bootstrap or not resolved_topic:
            raise RuntimeError('VENUE_OUTBOX_KAFKA_BOOTSTRAP and VENUE_OUTBOX_KAFKA_TOPIC (or VENUE_KAFKA_*) are required in kafka mode.')
        producer = _build_kafka_producer(bootstrap)

    sent_count = 0
    failed_count = 0

    with transaction.atomic():
        qs = VenueOutbox.objects.filter(state='PENDING').order_by('occurred_at')
        if connection.features.has_select_for_update_skip_locked:
            qs = qs.select_for_update(skip_locked=True)
        else:
            qs = qs.select_for_update()

        rows = list(qs[: max(1, int(batch_size))])
        for row in rows:
            attempt_no = row.attempts + 1
            message = _sanitized_outbox_message(row)
            try:
                if use_kafka and producer is not None:
                    producer.send(
                        resolved_topic,
                        key=str(row.venue_id).encode('utf-8'),
                        value=message,
                    ).get(timeout=5)
                emit_observation(
                    dataset='mlo.pipeline',
                    action=f'outbox-{row.event_type}',
                    outcome='success',
                    pipeline_name='venue-outbox-relay',
                    stage='kafka-publish' if use_kafka else 'observation-relay',
                    state='PUBLISHED',
                    attempt=attempt_no,
                    business_event_id=row.event_id,
                    request_id=row.request_id,
                    records_in=1,
                    records_out=1,
                )
                row.state = 'SENT'
                row.attempts = attempt_no
                row.last_error = ''
                row.published_at = timezone.now()
                row.save(update_fields=['state', 'attempts', 'last_error', 'published_at'])
                sent_count += 1
            except Exception as exc:
                err_code = type(exc).__name__[:80].upper()
                row.attempts = attempt_no
                row.last_error = f"{type(exc).__name__}: {exc}"[:120]
                if attempt_no >= max(1, int(max_attempts)):
                    row.state = 'FAILED'
                row.save(update_fields=['state', 'attempts', 'last_error'])
                emit_observation(
                    dataset='mlo.pipeline',
                    action=f'outbox-{row.event_type}',
                    outcome='failure',
                    pipeline_name='venue-outbox-relay',
                    stage='kafka-publish' if use_kafka else 'observation-relay',
                    state='FAILED' if row.state == 'FAILED' else 'RETRYING',
                    attempt=attempt_no,
                    business_event_id=row.event_id,
                    request_id=row.request_id,
                    error_code=err_code or 'OUTBOX_RELAY_FAILED',
                    retryable=row.state != 'FAILED',
                )
                failed_count += 1

    return {'processed': len(rows), 'sent': sent_count, 'failed': failed_count, 'mode': 'kafka' if use_kafka else 'observation'}


class Command(BaseCommand):
    help = 'Relay pending VenueOutbox domain events to Kafka or sanitized mlo.pipeline observations.'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=50, help='Maximum pending events processed per batch.')
        parser.add_argument('--mode', choices=['auto', 'kafka', 'observation'], default='auto', help='Delivery transport mode.')
        parser.add_argument('--max-attempts', type=int, default=5, help='Attempts before marking row FAILED.')
        parser.add_argument('--watch', action='store_true', help='Continuously poll pending outbox events.')
        parser.add_argument('--interval', type=float, default=2.0, help='Polling interval seconds in --watch mode.')
        parser.add_argument('--max-batches', type=int, default=0, help='Optional cap on batches in --watch mode (0 = unlimited).')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        mode = options['mode']
        max_attempts = options['max_attempts']
        watch = options['watch']
        interval = max(0.1, float(options['interval']))
        max_batches = max(0, int(options['max_batches']))

        batches = 0
        while True:
            result = relay_pending_batch(
                batch_size=batch_size,
                mode=mode,
                max_attempts=max_attempts,
            )
            batches += 1
            if result['processed'] or not watch:
                self.stdout.write(json.dumps(result, ensure_ascii=False))
            if not watch or (max_batches and batches >= max_batches):
                break
            time.sleep(interval)
