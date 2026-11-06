"""Local-only Kafka/Spark/Elasticsearch smoke fixture. Never use against AWS."""
import argparse
import json
import os
from datetime import datetime, timedelta
from importlib.resources import files
from urllib.error import HTTPError

from pipeline.weather.common import KST, OBS, FORECAST, digest, encode


def guard():
    if os.environ.get("MLO_LOCAL_INTEGRATION") != "1":
        raise RuntimeError("explicit_local_integration_flag_required")
    if os.environ.get("WEATHER_INDEX") != "weather-stream-current-v1-local":
        raise RuntimeError("refusing_nonlocal_weather_index")
    if os.environ.get("KAFKA_TOPIC") != "weather.v1.local":
        raise RuntimeError("refusing_nonlocal_kafka_topic")
    if os.environ.get("ES_URL") != "http://es-router:9200":
        raise RuntimeError("refusing_external_es_target")
    brokers = os.environ.get("KAFKA_BOOTSTRAP", "")
    if any(name not in brokers for name in ("kafka-01:9092", "kafka-02:9092", "kafka-03:9092")):
        raise RuntimeError("refusing_external_kafka_target")


def mapping_grid():
    info = json.loads(files("pipeline.weather").joinpath("mapping.json").read_text(encoding="utf-8"))
    region = next(
        r for r in info["regions"] if r["region_code"] == "1153051000"
    )
    return info["version"], region


def topic_report(topic):
    from kafka.admin import KafkaAdminClient
    client = KafkaAdminClient(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"],
        request_timeout_ms=15000,
    )
    try:
        meta = client.describe_topics([topic])[0]
        parts = meta["partitions"]
        replicas = [len(p["replicas"]) for p in parts]
        isrs = [len(p["isr"]) for p in parts]
        return dict(topic=topic, partitions=len(parts), replication=replicas, isr=isrs)
    finally:
        client.close()


def prepare():
    from pipeline.prepare_kafka import ensure_topic
    from pipeline.weather.prepare_es import ensure_index
    for prefix in ("KAFKA_", "FEEDBACK_KAFKA_"):
        result = ensure_topic(prefix)
        topic = os.environ[prefix + "TOPIC"]
        print(json.dumps({"topic_preparation": topic, **result}), flush=True)
    ensure_index()
    print(json.dumps({"weather_topic": topic_report(os.environ["KAFKA_TOPIC"])}))


def event(version, grid, now, source, kind, category, issued, target, raw, ordinal):
    raw = str(raw)
    source_hash = digest(["local-synthetic-source", source, category, issued, target, raw])
    payload = dict(
        schema_version=1, source=source, kind=kind, category=category,
        issued_at=issued, target_at=target, received_at=now,
        source_hash=source_hash, raw_path=None,
        run_id="local-stage3-synthetic", part=0,
        readings=[dict(nx=grid["nx"], ny=grid["ny"], raw=raw)],
        mapping_version=version, spatial_basis="dfs_5km",
        data_mode="synthetic", request_id=f"mlo-stage3-synthetic-{ordinal}",
    )
    payload["event_id"] = digest([payload["source_hash"], ordinal])
    return payload


def publish(marker):
    from kafka import KafkaProducer
    version, grid = mapping_grid()
    now = datetime.now(KST)
    issued = (now - timedelta(minutes=5)).isoformat()
    observation_time = now.replace(minute=0, second=0, microsecond=0)
    # Original project requires ALL 8 observation fields and ALL 11 forecast
    # fields for each of six targets; a partial sample is not a ready weather bundle.
    values = {
        "T1H": 21.8, "REH": 55, "RN1": 0, "WSD": 0.5,
        "UUU": 0, "VVV": 0, "VEC": 180, "PTY": 0,
        "SKY": 1, "LGT": 0, "POP": 0,
    }
    payloads = []
    # A synthetic rain observation avoids requiring a separate current-hour SKY
    # value to classify the present. Forecast slots stay clear with SKY=1.
    observations = {**values, "PTY": 1}
    for category in OBS:
        payloads.append(event(version, grid, now.isoformat(), "odam", "observation", category,
                              issued, observation_time.isoformat(), observations[category], len(payloads)))
    for hr in range(1, 7):
        target = observation_time + timedelta(hours=hr)
        for category in FORECAST:
            payloads.append(event(version, grid, now.isoformat(), "vsrt", "forecast", category,
                                  issued, target.isoformat(), values[category], len(payloads)))
    # Current-hour sky comes from the separate KIM cloud-cover supplement;
    # adding a seventh VSRT forecast slot would invalidate the six-slot bundle.
    cloud = event(version, grid, now.isoformat(), "KIMG", "forecast", "TCDC",
                  issued, observation_time.isoformat(), 0.05, len(payloads))
    cloud.update(query_lat=grid["lat"], query_lon=grid["lon"],
                 spatial_basis="selected_region_point_sample")
    payloads.append(cloud)
    producer = KafkaProducer(
        bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"],
        acks="all", retries=5, max_block_ms=15000,
        value_serializer=encode,
    )
    try:
        for item in payloads:
            item["run_id"] = f"local-stage3-synthetic-{marker}"
            producer.send(os.environ["KAFKA_TOPIC"], key=b"58_125", value=item).get(timeout=20)
        producer.flush(timeout=25)
    finally:
        producer.close(timeout=10)
    print(json.dumps(dict(
        fixture_type="synthetic_only", topic=os.environ["KAFKA_TOPIC"], messages=len(payloads),
        nx=grid["nx"], ny=grid["ny"], marker=marker,
    )), flush=True)


def verify():
    from pipeline.weather.es_current import request
    index = os.environ["WEATHER_INDEX"]
    try:
        result = request(os.environ["ES_URL"], "GET", f"/{index}/_doc/58_125")
    except HTTPError as err:
        raise RuntimeError(f"weather_document_missing_http_{err.code}") from None
    if not result.get("found"):
        raise RuntimeError("weather_document_not_found")
    source = result["_source"]
    rows = source["rows"]
    synthetic = [r for r in rows if r.get("data_mode") == "synthetic"]
    if not synthetic or not source.get("state_hash") or source["schema_version"] != 1:
        raise RuntimeError("synthetic_weather_not_processed_by_spark")
    categories = sorted({r["category"] for r in synthetic})
    if not {"T1H", "PTY", "SKY"}.issubset(categories):
        raise RuntimeError("expected_weather_categories_missing")
    print(json.dumps(dict(
        result="PASS", document_id="58_125", rows=len(rows), synthetic_rows=len(synthetic),
        categories=categories, revision=source["revision"],
        kafka=topic_report(os.environ["KAFKA_TOPIC"]),
    )), flush=True)


def status():
    from pipeline.weather.es_current import request
    health = request(os.environ["ES_URL"], "GET", "/_cluster/health")
    print(json.dumps(dict(
        elasticsearch_nodes=health["number_of_nodes"], elasticsearch_status=health["status"],
        weather_topic=topic_report(os.environ["KAFKA_TOPIC"]),
    )), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "publish", "verify", "status"])
    parser.add_argument("--marker", default="baseline")
    args = parser.parse_args()
    guard()
    {"prepare": prepare, "publish": lambda: publish(args.marker),
     "verify": verify, "status": status}[args.command]()


if __name__ == "__main__":
    main()
