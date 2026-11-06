"""Actual local Spark/Delta checkpoint and idempotent partial-sink retry lab."""

import json
from pathlib import Path
from pyspark.sql import SparkSession

root = Path("/data")
source = root / "input"
source.mkdir(exist_ok=True)
if not (source / "first.json").exists():
    (source / "first.json").write_text('{"id":1}\n')
else:
    (source / "second.json").write_text('{"id":2}\n')
spark = (
    SparkSession.builder.appName("B-local-delta-acceptance")
    .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
    .config(
        "spark.sql.catalog.spark_catalog",
        "org.apache.spark.sql.delta.catalog.DeltaCatalog",
    )
    .config("spark.sql.shuffle.partitions", "1")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")


def sinks(batch, batch_id):
    batch.write.format("delta").mode("append").option(
        "txnAppId", "B-local-fixture"
    ).option("txnVersion", batch_id).save("/data/delta")
    # The first sink has committed. The second sink fails once, before checkpoint completion.
    marker = root / "injected-failure"
    if not marker.exists():
        marker.write_text("first sink committed; second failed")
        raise RuntimeError("B injected partial sink failure")
    receipt = root / "second-sink.json"
    saved = json.loads(receipt.read_text()) if receipt.exists() else {}
    for row in batch.collect():
        saved[str(row.id)] = True
    receipt.write_text(json.dumps(saved))


try:
    query = (
        spark.readStream.schema("id INT")
        .json(str(source))
        .writeStream.foreachBatch(sinks)
        .option("checkpointLocation", "/data/checkpoint")
        .trigger(availableNow=True)
        .start()
    )
    query.awaitTermination()
    count = spark.read.format("delta").load("/data/delta").count()
    assert count == 2, f"expected 2 unique rows after retry, got {count}"
    assert json.loads((root / "second-sink.json").read_text()) == {"1": True, "2": True}
    print("B_DELTA_CHECKPOINT_RETRY_PASS", flush=True)
finally:
    spark.stop()
