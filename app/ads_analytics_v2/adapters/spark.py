"""Native Spark join/groupBy plus a bounded prototype streaming adapter.

The streaming source is Kafka. Only bounded micro-batches and aggregate results
cross the Control Driver boundary; no executor connects to MongoDB. The retained
event scan uses a disposable disk spool with bounded chunks and result rows;
full-history time and temporary disk cost remain proportional to retained input.
"""

from datetime import UTC, datetime

from ..errors import AnalyticsError, CapacityExceeded
from ..pipeline import consume_and_materialize
from ..reporting import COUNTERS, assemble, prepare, validate_query


def _report_chunk_with_spark(
    spark, events, *, cutoff, settled_through=None, max_events=100_000, max_rows=5_000, **scope
):
    from pyspark.sql import functions as f
    from pyspark.sql.types import StringType, StructField, StructType, TimestampType

    cutoff, settled, scope = validate_query(cutoff, settled_through, **scope)
    visible, eligible = prepare(events, cutoff, scope, max_events)
    if not eligible:
        return assemble(visible, [], cutoff, settled, scope)
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    fields = ["event_id", "delivery_id", "ad_id", "placement", "media_id", "event_type"]
    schema = StructType(
        [StructField(name, StringType(), False) for name in fields]
        + [StructField("occurred_at", TimestampType(), False)]
    )
    frame = spark.createDataFrame(
        [
            tuple(getattr(event, name) for name in fields) + (event.occurred_at,)
            for event in eligible
        ],
        schema,
    )
    impressions = frame.filter(f.col("event_type") == "VIEWABLE_IMPRESSION")
    clicks = frame.filter(f.col("event_type") == "CLICK")

    def metric_day(column):
        return f.date_format(f.from_utc_timestamp(column, "Asia/Seoul"), "yyyy-MM-dd")

    imp_facts = impressions.select(
        metric_day(f.col("occurred_at")).alias("date_kst"),
        "ad_id",
        "placement",
        "media_id",
        *[f.lit(int(name == "impressions")).cast("long").alias(name) for name in COUNTERS],
    )
    joined = clicks.alias("c").join(
        impressions.alias("i"), f.col("c.delivery_id") == f.col("i.delivery_id"), "left"
    )
    matched = f.col("i.occurred_at").isNotNull() & (
        f.col("i.occurred_at") <= f.col("c.occurred_at")
    )
    early = f.col("i.occurred_at").isNotNull() & (f.col("i.occurred_at") > f.col("c.occurred_at"))
    orphan = f.col("i.occurred_at").isNull()
    settled_condition = (
        f.lit(False) if settled is None else f.col("c.occurred_at") <= f.lit(settled)
    )
    click_facts = joined.select(
        f.when(matched, metric_day(f.col("i.occurred_at")))
        .otherwise(metric_day(f.col("c.occurred_at")))
        .alias("date_kst"),
        f.col("c.ad_id").alias("ad_id"),
        f.col("c.placement").alias("placement"),
        f.col("c.media_id").alias("media_id"),
        f.lit(0).cast("long").alias("impressions"),
        matched.cast("long").alias("matched_clicks"),
        early.cast("long").alias("early_clicks"),
        (orphan & settled_condition).cast("long").alias("unmatched_clicks"),
        (orphan & ~settled_condition).cast("long").alias("pending_clicks"),
    )
    facts = imp_facts.unionByName(click_facts)
    if scope.get("date_from"):
        facts = facts.filter(f.col("date_kst").between(scope["date_from"], scope["date_to"]))
    aggregates = facts.groupBy("date_kst", "ad_id", "placement", "media_id").agg(
        *[f.sum(name).alias(name) for name in COUNTERS]
    )
    rows = [row.asDict() for row in aggregates.limit(max_rows + 1).collect()]
    if len(rows) > max_rows:
        raise CapacityExceeded("REPORT_GROUP_LIMIT")
    return assemble(visible, rows, cutoff, settled, scope)



def report_with_spark(spark, events, *, cutoff, settled_through=None, max_events=None,
                      batch_size=1000, max_rows=5000, work_dir=None, **scope):
    from ..streaming_report import report_streaming

    return report_streaming(events, cutoff=cutoff, settled_through=settled_through,
        max_events=max_events, batch_size=batch_size, max_rows=max_rows, work_dir=work_dir,
        renderer=lambda chunk, **options: _report_chunk_with_spark(
            spark, chunk, max_rows=max_rows, **options), **scope)


def stream_from_kafka(
    spark,
    *,
    store_factory,
    bootstrap_servers,
    topic,
    checkpoint,
    security_options,
    max_batch=1_000,
    max_events=None,
):
    """Run explicitly on the Control Driver, with reviewed network and DB permissions.

    V1-to-V2 capture is not installed by this function. Late data is preserved in
    staging and a new provisional snapshot is computed. No watermark silently
    drops late messages. Operator settlement/reconciliation remains separate.
    """
    if (
        not bootstrap_servers
        or not topic
        or not checkpoint.startswith("s3a://")
        or not 1 <= max_batch <= 10_000
        or (max_events is not None and (type(max_events) is not int or max_events < 1))
    ):
        raise AnalyticsError("INVALID_STREAM_CONFIGURATION")
    if (
        security_options.get("kafka.security.protocol") != "SASL_SSL"
        or security_options.get("kafka.ssl.endpoint.identification.algorithm", "https") != "https"
    ):
        raise AnalyticsError("KAFKA_TLS_CONFIGURATION_REQUIRED")
    reader = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap_servers)
        .option("subscribe", topic)
        .option("startingOffsets", "earliest")
        .option("maxOffsetsPerTrigger", max_batch)
        .option("kafka.allow.auto.create.topics", "false")
        .option("failOnDataLoss", "true")
    )
    for key, value in security_options.items():
        if key not in {
            "kafka.security.protocol",
            "kafka.sasl.mechanism",
            "kafka.sasl.jaas.config",
            "kafka.ssl.truststore.location",
            "kafka.ssl.truststore.certificates",
            "kafka.ssl.truststore.password",
            "kafka.ssl.truststore.type",
            "kafka.ssl.endpoint.identification.algorithm",
        }:
            raise AnalyticsError("UNSUPPORTED_KAFKA_OPTION")
        reader = reader.option(key, value)
    spark.conf.set("spark.redaction.regex", "(?i)secret|password|token|jaas|access[.]key|certificates")
    rows = reader.load().selectExpr("key", "value")

    def save(batch, batch_id):
        del batch_id  # event_id, not an epoch-local batch number, is the deduplication key.
        messages = []
        for row in batch.limit(max_batch + 1).toLocalIterator():
            if len(messages) >= max_batch:
                raise CapacityExceeded("STREAM_BATCH_LIMIT")
            messages.append((bytes(row.key) if row.key is not None else None, bytes(row.value)))
        store = store_factory()
        try:
            consume_and_materialize(
                store,
                messages,
                cutoff=datetime.now(UTC),
                max_messages=max_batch,
                max_events=max_events,
                aggregate=lambda events, **options: report_with_spark(spark, events, **options),
            )
        finally:
            store.close()

    return rows.writeStream.foreachBatch(save).option("checkpointLocation", checkpoint).start()
