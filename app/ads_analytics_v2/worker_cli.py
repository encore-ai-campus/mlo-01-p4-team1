"""Django-independent Spark driver. Secrets/configuration come only from the environment."""

import argparse
import json
import os
import ssl
import sys
from pathlib import Path


def validate_environment(env):
    if env.get("ADS_V2_ENABLED") != "1":
        raise ValueError("ADS_V2_ENABLED를 명시적으로 설정하세요.")
    names = (
        "ADS_MONGODB_DATABASE",
        "ADS_V2_SOURCE_MONGODB_DATABASE",
        "ADS_V2_SINK_MONGODB_URI",
        "ADS_V2_SINK_MONGODB_DATABASE",
        "ADS_V2_KAFKA_BOOTSTRAP_SERVERS",
        "ADS_V2_KAFKA_TOPIC",
        "ADS_V2_KAFKA_USERNAME",
        "ADS_V2_KAFKA_PASSWORD",
        "ADS_V2_KAFKA_CA_FILE",
        "ADS_V2_KAFKA_PARTITIONS",
        "ADS_V2_KAFKA_REPLICATION_FACTOR",
        "ADS_V2_SPARK_MASTER",
        "ADS_V2_CHECKPOINT",
        "ADS_V2_SPARK_JARS",
    )
    missing = [name for name in names if not env.get(name)]
    if missing:
        raise ValueError("필수 환경 설정이 없습니다: " + ", ".join(missing))
    if (
        len(
            {
                env[name]
                for name in (
                    "ADS_MONGODB_DATABASE",
                    "ADS_V2_SOURCE_MONGODB_DATABASE",
                    "ADS_V2_SINK_MONGODB_DATABASE",
                )
            }
        )
        != 3
    ):
        raise ValueError("원본·발행 대기·수신 집계 DB 이름은 각각 달라야 합니다.")
    try:
        partitions = int(env["ADS_V2_KAFKA_PARTITIONS"])
        replication = int(env["ADS_V2_KAFKA_REPLICATION_FACTOR"])
    except ValueError:
        raise ValueError("Kafka partition·복제 수는 정수여야 합니다.") from None
    if not 1 <= partitions <= 32 or not 1 <= replication <= 5:
        raise ValueError("Kafka partition·복제 기대값 범위를 확인하세요.")
    trusted_certificates(env["ADS_V2_KAFKA_CA_FILE"])
    if not env["ADS_V2_CHECKPOINT"].startswith("s3a://"):
        raise ValueError("별도 s3a:// 체크포인트 경로가 필요합니다.")
    jars = env["ADS_V2_SPARK_JARS"].split(",")
    if not jars or any(
        not name or not Path(name).is_file() or not name.endswith(".jar")
        for name in jars
    ):
        raise ValueError(
            "검증된 Kafka·S3A 의존성 JAR 파일 목록을 명시하세요. 자동 다운로드하지 않습니다."
        )


def verify_topic(env):
    from kafka.admin import KafkaAdminClient

    client = KafkaAdminClient(
        bootstrap_servers=env["ADS_V2_KAFKA_BOOTSTRAP_SERVERS"],
        client_id="ads-v2-contract-check",
        security_protocol="SASL_SSL",
        sasl_mechanism="PLAIN",
        sasl_plain_username=env["ADS_V2_KAFKA_USERNAME"],
        sasl_plain_password=env["ADS_V2_KAFKA_PASSWORD"],
        ssl_cafile=env["ADS_V2_KAFKA_CA_FILE"],
        ssl_check_hostname=True,
        request_timeout_ms=15000,
    )
    try:
        topics = client.describe_topics([env["ADS_V2_KAFKA_TOPIC"]])
    finally:
        client.close()
    if (
        len(topics) != 1
        or topics[0].get("topic") != env["ADS_V2_KAFKA_TOPIC"]
        or topics[0].get("error_code", 0)
    ):
        raise ValueError(
            "기존 Kafka 토픽을 확인할 수 없습니다. 자동 생성하지 않습니다."
        )
    partitions = topics[0].get("partitions", [])
    if len(partitions) != int(env["ADS_V2_KAFKA_PARTITIONS"]) or any(
        len(row.get("replicas", [])) != int(env["ADS_V2_KAFKA_REPLICATION_FACTOR"])
        or row.get("error_code", 0)
        for row in partitions
    ):
        raise ValueError("Kafka partition·복제 계약이 실제 메타데이터와 다릅니다.")


def sink_store(env):
    from pymongo import MongoClient

    from ads_analytics_v2.adapters.mongo import MongoStore

    client = MongoClient(
        env["ADS_V2_SINK_MONGODB_URI"],
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=15000,
    )
    store = MongoStore(client[env["ADS_V2_SINK_MONGODB_DATABASE"]])
    try:
        store.prepare_indexes()
    except Exception:
        client.close()
        raise
    store.close = client.close  # The streaming driver owns this per-batch client.
    return store


def run_spark(env):
    from pyspark.sql import SparkSession

    from ads_analytics_v2.adapters.spark import stream_from_kafka

    verify_topic(env)
    certificates = trusted_certificates(env["ADS_V2_KAFKA_CA_FILE"])
    spark = (
        SparkSession.builder.appName("ads-analytics-v2")
        .master(env["ADS_V2_SPARK_MASTER"])
        .config("spark.jars", env["ADS_V2_SPARK_JARS"])
        .config(
            "spark.redaction.regex",
            "(?i)secret|password|token|jaas|certificates|access[.]key",
        )
        .getOrCreate()
    )

    def factory():
        return sink_store(env)

    security = {
        "kafka.security.protocol": "SASL_SSL",
        "kafka.sasl.mechanism": "PLAIN",
        "kafka.sasl.jaas.config": "org.apache.kafka.common.security.plain.PlainLoginModule required "
        + 'username="'
        + jaas_escape(env["ADS_V2_KAFKA_USERNAME"])
        + '" password="'
        + jaas_escape(env["ADS_V2_KAFKA_PASSWORD"])
        + '";',
        "kafka.ssl.endpoint.identification.algorithm": "https",
        # Kafka's PEM configuration travels with each consumer's options, including
        # consumers on remote executors. Do not pass a driver-only absolute path.
        "kafka.ssl.truststore.certificates": certificates,
        "kafka.ssl.truststore.type": "PEM",
    }
    query = None
    try:
        query = stream_from_kafka(
            spark,
            store_factory=factory,
            bootstrap_servers=env["ADS_V2_KAFKA_BOOTSTRAP_SERVERS"],
            topic=env["ADS_V2_KAFKA_TOPIC"],
            checkpoint=env["ADS_V2_CHECKPOINT"],
            security_options=security,
        )
        print(
            json.dumps(
                {
                    "stage": "spark",
                    "key_contract": "delivery_id",
                    "status": "STARTED_PROVISIONAL",
                }
            ),
            flush=True,
        )
        query.awaitTermination()
    finally:
        if query is not None:
            query.stop()
        spark.stop()


def jaas_escape(value):
    return value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))


def trusted_certificates(filename):
    """Read public PEM CA certificates only; never send private key material."""
    try:
        path = Path(filename)
        if not path.is_file() or not 1 <= path.stat().st_size <= 1_048_576:
            raise ValueError
        payload = path.read_text(encoding="ascii")
        if "PRIVATE KEY" in payload or "-----BEGIN CERTIFICATE-----" not in payload:
            raise ValueError
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=payload)
        return payload
    except (OSError, UnicodeError, ValueError, ssl.SSLError):
        raise ValueError(
            "공개 PEM CA 인증서만 담은 1MiB 이하의 유효한 파일이 필요합니다."
        ) from None


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(
        description="광고 V2 Spark driver. 실제 접속은 명시 실행에만 수행합니다."
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="환경 구조만 확인합니다. 외부 서비스 연결은 하지 않습니다.",
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        validate_environment(env)
    except ValueError as error:
        # Validation messages contain setting names only, never configured values.
        print(str(error), file=sys.stderr)
        return 2
    if args.check_config:
        print(
            json.dumps(
                {"configuration_valid": True, "external_services_verified": False}
            )
        )
        return 0
    try:
        run_spark(env)
        return 0
    except KeyboardInterrupt:
        return 130
    except ImportError:
        print(
            "선택 Kafka/PyMongo/Spark 런타임 의존성을 준비하세요. 자동 설치하지 않습니다.",
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 -- third-party runtime errors may contain credentials/URIs.
        print(
            "Spark 작업이 실패했습니다. 비밀값을 제외한 운영 상태를 확인하세요.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
