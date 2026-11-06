"""Synthetic Spark artifact, deliberately not the missing Track2 application."""

import hashlib
import json
from pathlib import Path

from pyspark.sql import SparkSession, Window
from pyspark.sql.functions import col, row_number

root = Path("/data")
spark = (
    SparkSession.builder.appName("B-venues-synthetic-artifact")
    .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
    .config(
        "spark.sql.catalog.spark_catalog",
        "org.apache.spark.sql.delta.catalog.DeltaCatalog",
    )
    .config("spark.sql.shuffle.partitions", "1")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")


def commit(batch, batch_id):
    latest = (
        batch.withColumn(
            "rank",
            row_number().over(Window.partitionBy("id").orderBy(col("revision").desc())),
        )
        .where("rank = 1")
        .drop("rank")
    )
    if (root / "delta/_delta_log").is_dir():
        latest.createOrReplaceTempView("fixture_latest")
        batch.sparkSession.sql(
            """MERGE INTO delta.`/data/delta` t USING fixture_latest s ON t.id = s.id
            WHEN MATCHED AND s.revision > t.revision THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *"""
        )
    else:
        latest.write.format("delta").save("/data/delta")
    if not (root / "failed-once").exists():
        (root / "failed-once").write_text(
            "Delta committed; external sink deliberately incomplete"
        )
        raise RuntimeError("B_CATALOG_PARTIAL_SINK_FAILURE")


try:
    stream = (
        spark.readStream.schema(
            "id STRING, revision LONG, deleted BOOLEAN, label STRING"
        )
        .json("/data/raw")
        .writeStream.foreachBatch(commit)
        .option("checkpointLocation", "/data/aws/local/g1/venues-catalog/checkpoint")
        .trigger(availableNow=True)
        .start()
    )
    stream.awaitTermination()
    rows = spark.read.format("delta").load("/data/delta").orderBy("id").collect()
    documents = [
        {
            "id": r.id,
            "revision": r.revision,
            "deleted": r.deleted,
            "document": {} if r.deleted else {"label": r.label},
        }
        for r in rows
    ]
    assert [(r.id, r.revision, r.deleted) for r in rows] == [
        ("v1", 2, True),
        ("v2", 2, False),
        ("v3", 1, False),
    ]
    export = root / "export"
    export.mkdir(exist_ok=True)
    data = ("\n".join(json.dumps(d, sort_keys=True) for d in documents) + "\n").encode()
    (export / "part.ndjson").write_bytes(data)
    canonical = json.dumps(
        documents, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    manifest = {
        "version": "0.2.0",
        "query": "venues-catalog",
        "release_id": "fixture-r2",
        "dataset_version": "fixture-d2",
        "complete": True,
        "schema_sha256": hashlib.sha256(json.dumps(
            {"version": "catalog-search/1", "properties": {"label": {"type": "keyword"}}},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
        "files": {"part.ndjson": hashlib.sha256(data).hexdigest()},
        "records": 3,
        "documents_sha256": hashlib.sha256(canonical).hexdigest(),
    }
    (export / "manifest.json").write_text(json.dumps(manifest))
    print("B_CATALOG_DELTA_RESTART_DUPLICATE_LATE_DELETE_PASS", flush=True)
finally:
    spark.stop()
