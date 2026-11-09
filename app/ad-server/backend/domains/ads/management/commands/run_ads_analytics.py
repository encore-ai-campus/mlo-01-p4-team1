"""Explicit V2 capture/publish/Spark entrypoints; no provisioning or demo promotion."""

import json
import os
import time
from pathlib import Path

from ads_analytics_v2.adapters.mongo import MongoStore
from ads_analytics_v2.events import identifier
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pymongo import MongoClient


def configuration(stage):
    if not settings.ADS_V2_ENABLED:
        raise CommandError("V2가 비활성 상태입니다. ADS_V2_ENABLED를 명시적으로 설정하세요.")
    required = [
        "ADS_MONGODB_URI",
        "ADS_MONGODB_DATABASE",
        "ADS_V2_SOURCE_MONGODB_URI",
        "ADS_V2_SOURCE_MONGODB_DATABASE",
        "ADS_V2_SINK_MONGODB_URI",
        "ADS_V2_SINK_MONGODB_DATABASE",
        "ADS_V2_SOURCE_ID",
        "ADS_V2_MEDIA_ID",
    ]
    if stage in ("publish", "spark"):
        required += [
            "ADS_V2_KAFKA_BOOTSTRAP_SERVERS",
            "ADS_V2_KAFKA_TOPIC",
            "ADS_V2_KAFKA_USERNAME",
            "ADS_V2_KAFKA_PASSWORD",
            "ADS_V2_KAFKA_CA_FILE",
        ]
    if stage == "spark":
        required += ["ADS_V2_SPARK_MASTER", "ADS_V2_CHECKPOINT"]
    missing = [name for name in required if not getattr(settings, name, "")]
    if missing:
        raise CommandError("필수 설정이 없습니다: " + ", ".join(missing))
    databases = [
        settings.ADS_MONGODB_DATABASE,
        settings.ADS_V2_SOURCE_MONGODB_DATABASE,
        settings.ADS_V2_SINK_MONGODB_DATABASE,
    ]
    if len(set(databases)) != 3:
        raise CommandError("원본 광고·V2 발행 대기·V2 수신 집계 DB 이름은 각각 달라야 합니다.")
    if not 1 <= settings.ADS_V2_INTERVAL_SECONDS <= 3600:
        raise CommandError("집계 주기는 1~3600초여야 합니다.")
    for value in (settings.ADS_V2_SOURCE_ID, settings.ADS_V2_MEDIA_ID):
        identifier(value)
    if stage in ("publish", "spark"):
        identifier(settings.ADS_V2_KAFKA_TOPIC)
        if (
            not 1 <= settings.ADS_V2_KAFKA_PARTITIONS <= 32
            or not 1 <= settings.ADS_V2_KAFKA_REPLICATION_FACTOR <= 5
        ):
            raise CommandError(
                "기존 Kafka 토픽의 기대 partition 수(1~32)·복제 수(1~5)를 명시하세요."
            )
        if not Path(settings.ADS_V2_KAFKA_CA_FILE).is_file():
            raise CommandError("Kafka CA 파일을 찾을 수 없습니다.")
    if stage == "spark" and not settings.ADS_V2_CHECKPOINT.startswith("s3a://"):
        raise CommandError("Spark 체크포인트는 별도 s3a:// 경로여야 합니다.")


def kafka_contract():
    """Describe an existing topic and fail on mismatch. Never create/alter a topic."""
    from kafka.admin import KafkaAdminClient

    client = KafkaAdminClient(
        bootstrap_servers=settings.ADS_V2_KAFKA_BOOTSTRAP_SERVERS,
        client_id="ads-v2-contract-check",
        security_protocol="SASL_SSL",
        sasl_mechanism="PLAIN",
        sasl_plain_username=settings.ADS_V2_KAFKA_USERNAME,
        sasl_plain_password=settings.ADS_V2_KAFKA_PASSWORD,
        ssl_cafile=settings.ADS_V2_KAFKA_CA_FILE,
        ssl_check_hostname=True,
        request_timeout_ms=15000,
    )
    try:
        topics = client.describe_topics([settings.ADS_V2_KAFKA_TOPIC])
    finally:
        client.close()
    if (
        len(topics) != 1
        or topics[0].get("topic") != settings.ADS_V2_KAFKA_TOPIC
        or topics[0].get("error_code", 0)
    ):
        raise CommandError("기존 Kafka 토픽을 확인할 수 없습니다. 자동 생성하지 않습니다.")
    partitions = topics[0].get("partitions", [])
    if len(partitions) != settings.ADS_V2_KAFKA_PARTITIONS or any(
        len(row.get("replicas", [])) != settings.ADS_V2_KAFKA_REPLICATION_FACTOR
        or row.get("error_code", 0)
        for row in partitions
    ):
        raise CommandError("Kafka 토픽 partition·복제 계약이 실제 메타데이터와 다릅니다.")


def mongo(uri):
    return MongoClient(
        uri,
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=15000,
    )


def next_delay(stage, result, counts, *, interval, publish_limit):
    """Drain bounded work fairly; only idle/sweep-complete cycles wait a full interval."""
    if stage == "collect":
        return interval if result["sweep_complete"] else 0.01
    if result["claimed"] >= publish_limit:
        return 0.01
    if counts.get("PENDING", 0) or counts.get("SENDING", 0):
        return min(interval, 1)
    return interval


class Command(BaseCommand):
    help = (
        "광고 V2 수집·전송·Spark 집계를 명시 실행합니다. 기존 원장과 합성 자료는 변경하지 않습니다."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--stage", choices=("collect", "publish", "spark", "dead", "requeue"), required=True
        )
        parser.add_argument(
            "--watch", action="store_true", help="collect/publish를 주기적으로 반복합니다."
        )
        parser.add_argument("--page-size", type=int, default=1000)
        parser.add_argument("--publish-limit", type=int, default=100)
        parser.add_argument(
            "--event-id",
            action="append",
            default=[],
            help="requeue에서만 사용합니다. DEAD 사건 ID를 반복 지정하세요.",
        )

    def handle(self, *args, **options):
        stage = options["stage"]
        if not 1 <= options["page_size"] <= 10000 or not 1 <= options["publish_limit"] <= 10000:
            raise CommandError("page-size와 publish-limit은 1~10000이어야 합니다.")
        event_ids = options.get("event_id", [])
        if stage == "requeue" and not 1 <= len(event_ids) <= 100:
            raise CommandError("DEAD 재처리에는 --event-id를 1~100개 명시하세요.")
        if stage != "requeue" and event_ids:
            raise CommandError("--event-id는 requeue에서만 사용할 수 있습니다.")
        if stage in ("dead", "requeue") and options["watch"]:
            raise CommandError("DEAD 조회·재처리는 명시한 한 번만 실행합니다.")
        try:
            configuration(stage)
            if stage == "publish":
                kafka_contract()
            if stage == "spark":
                return self.spark()
            self.loop(stage, options)
        except (CommandError, KeyboardInterrupt):
            raise
        except ImportError:
            raise CommandError(
                "선택 런타임 의존성이 없습니다. 검증된 Kafka/Spark 환경에서 실행하세요."
            ) from None
        except Exception:
            # Client exceptions may contain URIs, credentials or producer configuration.
            raise CommandError(
                "광고 V2 작업이 실패했습니다. 비밀값을 제외한 운영 상태를 점검하세요."
            ) from None

    def loop(self, stage, options):
        from ads_analytics_v2.adapters.kafka import KafkaBroker
        from ads_analytics_v2.adapters.source_snapshot import capture_v1_page
        from ads_analytics_v2.transport import publish_once

        source_client = mongo(settings.ADS_V2_SOURCE_MONGODB_URI)
        original_client = None
        broker = None
        try:
            store = MongoStore(source_client[settings.ADS_V2_SOURCE_MONGODB_DATABASE])
            if stage in ("dead", "requeue"):
                if stage == "requeue":
                    result = {"requeued": store.requeue_dead(options["event_id"], now=time.time())}
                else:
                    result = {"dead_letters": store.dead_letters(limit=100)}
                result["queue"] = store.queue_counts()
                self.stdout.write(json.dumps({"stage": stage, **result}))
                return
            store.prepare_indexes()
            if stage == "collect":
                original_client = mongo(settings.ADS_MONGODB_URI)
            else:
                broker = KafkaBroker.connect(
                    bootstrap_servers=settings.ADS_V2_KAFKA_BOOTSTRAP_SERVERS,
                    topic=settings.ADS_V2_KAFKA_TOPIC,
                    username=settings.ADS_V2_KAFKA_USERNAME,
                    password=settings.ADS_V2_KAFKA_PASSWORD,
                    ca_file=settings.ADS_V2_KAFKA_CA_FILE,
                )
            while True:
                if stage == "collect":
                    result = capture_v1_page(
                        original_client[settings.ADS_MONGODB_DATABASE]["ad_events"],
                        store,
                        source_id=settings.ADS_V2_SOURCE_ID,
                        media_id=settings.ADS_V2_MEDIA_ID,
                        cutoff=timezone.now(),
                        page_size=options["page_size"],
                        now=time.time(),
                    )
                    result = {
                        key: result[key]
                        for key in (
                            "source_rows",
                            "inserted",
                            "sweep_complete",
                            "source_completeness_verified",
                        )
                    }
                else:
                    result = publish_once(store, broker, limit=options["publish_limit"])
                self.stdout.write(
                    json.dumps({"stage": stage, "key_contract": "delivery_id", **result})
                )
                if not options["watch"]:
                    break
                counts = (
                    store.queue_counts()
                    if stage == "publish" and result["claimed"] < options["publish_limit"]
                    else {}
                )
                time.sleep(
                    next_delay(
                        stage,
                        result,
                        counts,
                        interval=settings.ADS_V2_INTERVAL_SECONDS,
                        publish_limit=options["publish_limit"],
                    )
                )
        finally:
            if broker:
                broker.close()
            if original_client:
                original_client.close()
            source_client.close()

    def spark(self):
        from ads_analytics_v2.worker_cli import run_spark, validate_environment

        env = dict(os.environ)
        for name in dir(settings):
            if name.startswith("ADS_V2_"):
                env[name] = str(getattr(settings, name))
        env["ADS_V2_ENABLED"] = "1" if settings.ADS_V2_ENABLED else "0"
        env["ADS_MONGODB_DATABASE"] = settings.ADS_MONGODB_DATABASE
        validate_environment(env)
        run_spark(env)
