import json
import logging
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from backend.venues.dl_engine.feature_encoder_18d import get_feature_encoder
from backend.venues.dl_engine.micro_two_tower import get_micro_two_tower
from backend.venues.dl_engine.realtime_user_memory import get_user_memory
from backend.venues.repositories.csv_lazy_repo import get_venue_repo
from backend.venues.streaming.observability import emit_observation

logger = logging.getLogger(__name__)


class AsyncKafkaDispatcher:
    """
    유저 행동 이벤트 비동기 논블로킹(Non-blocking) 디스패처.
    - queue.Queue(maxsize=10000)를 활용해 HTTP 요청 스레드 대기 시간 5ms 이내 보장.
    - 백그라운드 워커가 큐를 소비하며 실시간 16D DL 유저 메모리 즉시 갱신.
    - VENUE_KAFKA_BOOTSTRAP / VENUE_KAFKA_TOPIC 설정 시 실제 KafkaProducer 발행, 미설정 시 관측/로컬 폴백 계측.
    """

    def __init__(self, queue_size: int = 10000, max_workers: int = 4):
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='venue_event_worker')
        self._running = True
        self._worker_threads = []
        self._stats_lock = threading.Lock()
        self._producer_lock = threading.Lock()
        self._producer = None
        self._enqueued_count = 0
        self._processed_count = 0
        self._dropped_count = 0
        self._kafka_sent_count = 0
        self._kafka_failed_count = 0
        self._last_event_at: Optional[float] = None
        self._last_error = ''

        # 백그라운드 큐 소비 스레드 가동
        for i in range(max_workers):
            t = threading.Thread(target=self._worker_loop, name=f'async_kafka_consumer_{i}', daemon=True)
            t.start()
            self._worker_threads.append(t)

        logger.info(f"[AsyncKafkaDispatcher] Initialized with {max_workers} background workers, queue_size={queue_size}")

    def enqueue_behavior_event(
        self,
        user_id: str,
        venue_id: str,
        behavior_type: str,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        HTTP 요청 스레드에서 호출하는 논블로킹 큐 등록 메서드.
        put_nowait()를 사용하여 블로킹 없이 즉시 적재.
        """
        event_payload = {
            'event_id': str(uuid.uuid4()),
            'user_id': user_id,
            'venue_id': venue_id,
            'behavior_type': behavior_type.upper(),
            'timestamp': time.time(),
            'extra_data': extra_data or {},
        }

        try:
            self._queue.put_nowait(event_payload)
            with self._stats_lock:
                self._enqueued_count += 1
                self._last_event_at = event_payload['timestamp']
            return True
        except queue.Full:
            with self._stats_lock:
                self._dropped_count += 1
                self._last_error = 'QUEUE_FULL'
            logger.warning("[AsyncKafkaDispatcher] Queue is full! Dropping behavior event.")
            emit_observation(
                dataset='mlo.pipeline',
                action='venue-behavior-enqueue',
                outcome='failure',
                pipeline_name='venue-behavior-stream',
                stage='enqueue',
                state='FAILED',
                error_code='QUEUE_FULL',
                retryable=True,
                business_event_id=event_payload['event_id'],
            )
            return False

    def _worker_loop(self) -> None:
        """백그라운드 워커: 큐에서 이벤트를 꺼내 비동기 처리"""
        user_memory = get_user_memory()
        encoder = get_feature_encoder()
        two_tower = get_micro_two_tower()
        repo = get_venue_repo()

        while self._running:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                user_id = event['user_id']
                venue_id = event['venue_id']
                behavior_type = event['behavior_type']

                # 1. 대상 매장 정보 조회 및 18D -> 16D 임베딩 산출
                venue_data = repo.get_venue_by_id(venue_id)
                if venue_data:
                    venue_feat = encoder.encode_venue(venue_data)
                    venue_emb = two_tower.forward_venue(venue_feat)

                    # 2. 실시간 16D 취향 벡터 이동
                    user_memory.update_user_vector(
                        user_id=user_id,
                        behavior_type=behavior_type,
                        venue_embedding=venue_emb,
                    )

                # 3. Kafka 발행 또는 로컬 관측 폴백
                self._dispatch_to_kafka(event)
                with self._stats_lock:
                    self._processed_count += 1

            except Exception as e:
                with self._stats_lock:
                    self._last_error = type(e).__name__[:80]
                logger.error(f"[AsyncKafkaDispatcher] Error processing event: {type(e).__name__}")
            finally:
                self._queue.task_done()

    def _get_kafka_producer(self):
        bootstrap = os.environ.get('VENUE_KAFKA_BOOTSTRAP', '').strip()
        topic = os.environ.get('VENUE_KAFKA_TOPIC', '').strip()
        if not bootstrap or not topic:
            return None, ''
        with self._producer_lock:
            if self._producer is None:
                from kafka import KafkaProducer
                from pipeline.kafka_config import python_options

                self._producer = KafkaProducer(
                    bootstrap_servers=bootstrap,
                    acks='all',
                    retries=3,
                    **python_options('VENUE_KAFKA_'),
                    value_serializer=lambda value: json.dumps(value, ensure_ascii=False).encode('utf-8'),
                )
        return self._producer, topic

    def _dispatch_to_kafka(self, event: Dict[str, Any]) -> None:
        """설정된 경우 Kafka 토픽으로 발행하고, 미설정 시 sanitized 관측 계측으로 기록"""
        producer, topic = self._get_kafka_producer()
        if producer is None or not topic:
            logger.debug(
                "[Kafka-Fallback] Recorded behavior event locally: %s on %s",
                event.get('behavior_type'),
                event.get('venue_id'),
            )
            return

        sanitized_event = {
            'event_id': event.get('event_id'),
            'behavior_type': event.get('behavior_type'),
            'venue_id': event.get('venue_id'),
            'timestamp': event.get('timestamp'),
        }
        try:
            key = str(event.get('event_id') or '').encode('utf-8')
            producer.send(topic, key=key, value=sanitized_event).get(timeout=5)
            with self._stats_lock:
                self._kafka_sent_count += 1
        except Exception as exc:
            with self._stats_lock:
                self._kafka_failed_count += 1
                self._last_error = type(exc).__name__[:80]
            logger.warning("[AsyncKafkaDispatcher] Kafka publish failed (%s)", type(exc).__name__)
            emit_observation(
                dataset='mlo.pipeline',
                action='venue-behavior-kafka-publish',
                outcome='failure',
                pipeline_name='venue-behavior-stream',
                stage='publish',
                state='FAILED',
                error_code='KAFKA_PUBLISH_FAILED',
                retryable=True,
                business_event_id=event.get('event_id'),
            )

    def get_stats(self) -> Dict[str, Any]:
        """어드민 모니터링 API용 디스패처 상태 스냅샷"""
        kafka_configured = bool(
            os.environ.get('VENUE_KAFKA_BOOTSTRAP', '').strip()
            and os.environ.get('VENUE_KAFKA_TOPIC', '').strip()
        )
        with self._stats_lock:
            return {
                'mode': 'kafka' if kafka_configured else 'local_fallback',
                'kafka_configured': kafka_configured,
                'queue_depth': self._queue.qsize(),
                'enqueued_count': self._enqueued_count,
                'processed_count': self._processed_count,
                'dropped_count': self._dropped_count,
                'kafka_sent_count': self._kafka_sent_count,
                'kafka_failed_count': self._kafka_failed_count,
                'last_event_at': self._last_event_at,
                'last_error': self._last_error,
            }

    def shutdown(self) -> None:
        """종료 처리"""
        self._running = False
        with self._producer_lock:
            if self._producer is not None:
                try:
                    self._producer.close(timeout=5)
                except Exception:
                    pass
                self._producer = None
        self._executor.shutdown(wait=False)


# 글로벌 싱글톤
_dispatcher_instance: Optional[AsyncKafkaDispatcher] = None
_disp_lock = threading.Lock()


def get_async_dispatcher() -> AsyncKafkaDispatcher:
    global _dispatcher_instance
    if _dispatcher_instance is None:
        with _disp_lock:
            if _dispatcher_instance is None:
                _dispatcher_instance = AsyncKafkaDispatcher()
    return _dispatcher_instance


def enqueue_behavior_event(
    user_id: str,
    venue_id: str,
    behavior_type: str,
    extra_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """편의 함수: 비동기 큐에 유저 행동 적재"""
    return get_async_dispatcher().enqueue_behavior_event(
        user_id=user_id,
        venue_id=venue_id,
        behavior_type=behavior_type,
        extra_data=extra_data,
    )
