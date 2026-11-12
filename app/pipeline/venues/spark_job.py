"""Track 2 venues-catalog Spark + Delta Lakehouse job (raw -> bronze -> silver -> gold).

Contracts implemented:
- Raw source: raw/venues/<dataset_version>/
- Delta tables: lake/venues/<dataset_version>/v1/{bronze,silver,gold}/
- Checkpoint: aws/<environment>/<generation>/venues-catalog/checkpoint/
- Releases shared SparkContext slot on completion (`spark.stop()`).
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from pipeline.kafka_config import spark_options
from pipeline.venues.catalog_normalize import DEFAULT_DATASET_VERSION


def _table_path(lake_base: str, dataset_version: str, layer: str) -> str:
    base = lake_base.rstrip('/')
    if base.endswith(f'/{dataset_version}/v1'):
        return f'{base}/{layer}'
    return f'{base}/venues/{dataset_version}/v1/{layer}'


def _has_delta_log(spark, path: str) -> bool:
    try:
        hadoop_conf = spark._jsc.hadoopConfiguration()
        jvm = spark._jvm
        fs_path = jvm.org.apache.hadoop.fs.Path(path.rstrip('/') + '/_delta_log')
        fs = fs_path.getFileSystem(hadoop_conf)
        return bool(fs.isDirectory(fs_path))
    except Exception:
        return False


def _merge_or_save_silver(spark, silver_df, silver_path: str) -> None:
    if _has_delta_log(spark, silver_path):
        silver_df.createOrReplaceTempView('venues_silver_stage')
        spark.sql(
            f"""
            MERGE INTO delta.`{silver_path}` AS target
            USING venues_silver_stage AS source
            ON target.venue_id = source.venue_id
            WHEN MATCHED AND (
                source.revision > target.revision
                OR (source.revision = target.revision AND source.updated_at >= target.updated_at)
            ) THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
            """
        )
    else:
        silver_df.write.format('delta').mode('overwrite').save(silver_path)


def _process_microbatch(batch_df, batch_id: int, *, lake_base: str, dataset_version: str) -> None:
    from pyspark.sql import Window
    from pyspark.sql import functions as f

    if batch_df.rdd.isEmpty():
        return

    spark = batch_df.sparkSession
    bronze_path = _table_path(lake_base, dataset_version, 'bronze')
    silver_path = _table_path(lake_base, dataset_version, 'silver')
    gold_path = _table_path(lake_base, dataset_version, 'gold')

    bronze_df = batch_df.withColumn('ingested_at', f.current_timestamp()).withColumn('batch_id', f.lit(int(batch_id)))
    (
        bronze_df.write.format('delta')
        .mode('append')
        .option('txnAppId', 'venues-catalog-bronze')
        .option('txnVersion', str(batch_id))
        .save(bronze_path)
    )

    dedup_window = Window.partitionBy('venue_id').orderBy(f.col('revision').desc(), f.col('updated_at').desc())
    silver_df = (
        batch_df.withColumn('_rank', f.row_number().over(dedup_window))
        .where(f.col('_rank') == 1)
        .drop('_rank')
        .withColumn('is_searchable', f.col('status_code') != f.lit('CLOSED_HIDDEN'))
        .withColumn('deleted', f.col('status_code') == f.lit('CLOSED_HIDDEN'))
    )
    _merge_or_save_silver(spark, silver_df, silver_path)

    full_silver = spark.read.format('delta').load(silver_path)
    searchable_df = full_silver.where(f.col('is_searchable') == f.lit(True))
    gold_df = searchable_df.groupBy('gu_name').agg(
        f.count('*').alias('searchable_venues'),
        f.sum(f.when(f.col('category') == f.lit('PUB'), 1).otherwise(0)).alias('pub_count'),
        f.sum(f.when(f.col('category') != f.lit('PUB'), 1).otherwise(0)).alias('cafe_count'),
        f.sum(f.when(f.col('is_partner') == f.lit(True), 1).otherwise(0)).alias('partner_count'),
        f.round(f.avg(f.when(f.col('avg_price') > 0, f.col('avg_price')))).alias('avg_price_krw'),
    )
    gold_df.write.format('delta').mode('overwrite').option('overwriteSchema', 'true').save(gold_path)

    print(
        json.dumps(
            {
                'pipeline': 'venues-catalog',
                'batch_id': batch_id,
                'dataset_version': dataset_version,
                'bronze_path': bronze_path,
                'silver_path': silver_path,
                'gold_path': gold_path,
                'recorded_at': datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _catalog_schema():
    from pyspark.sql.types import BooleanType, DoubleType, IntegerType, LongType, StringType, StructField, StructType

    return StructType([
        StructField('venue_id', StringType(), False),
        StructField('dataset_version', StringType(), True),
        StructField('venue_name', StringType(), True),
        StructField('gu_name', StringType(), True),
        StructField('dong_name', StringType(), True),
        StructField('road_address', StringType(), True),
        StructField('category', StringType(), True),
        StructField('sub_category', StringType(), True),
        StructField('store_type', StringType(), True),
        StructField('brand_name', StringType(), True),
        StructField('price_tier', IntegerType(), True),
        StructField('avg_price', IntegerType(), True),
        StructField('latitude', DoubleType(), True),
        StructField('longitude', DoubleType(), True),
        StructField('is_partner', BooleanType(), True),
        StructField('is_open_now', BooleanType(), True),
        StructField('primary_weather_affinity', StringType(), True),
        StructField('status_code', StringType(), True),
        StructField('revision', LongType(), True),
        StructField('menu_count', IntegerType(), True),
        StructField('source_sha256', StringType(), True),
        StructField('updated_at', StringType(), True),
    ])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Track 2 venues-catalog Spark Delta Lakehouse projection.')
    parser.add_argument('--mode', choices=['batch', 'stream', 'kafka'], default=os.environ.get('VENUE_PIPELINE_MODE', 'batch'))
    parser.add_argument('--dataset-version', default=os.environ.get('VENUE_DATASET_VERSION', DEFAULT_DATASET_VERSION))
    args = parser.parse_args(argv)

    lake_base = os.environ.get('VENUE_LAKE_BASE_PATH', '').strip()
    checkpoint_path = os.environ.get('VENUE_CHECKPOINT_PATH', os.environ.get('CHECKPOINT_PATH', '')).strip()
    raw_path = os.environ.get('VENUE_RAW_PATH', '').strip()

    if not lake_base or not checkpoint_path:
        raise RuntimeError('VENUE_LAKE_BASE_PATH_and_CHECKPOINT_PATH_required')
    if args.mode in ('batch', 'stream') and not raw_path:
        raise RuntimeError('VENUE_RAW_PATH_required')
    if args.mode == 'kafka' and (not os.environ.get('VENUE_KAFKA_BOOTSTRAP') or not os.environ.get('VENUE_KAFKA_TOPIC')):
        raise RuntimeError('VENUE_KAFKA_BOOTSTRAP_and_VENUE_KAFKA_TOPIC_required')

    from pyspark.sql import SparkSession
    from pyspark.sql import functions as f

    spark = (
        SparkSession.builder.appName('venues-catalog')
        .config('spark.sql.extensions', 'io.delta.sql.DeltaSparkSessionExtension')
        .config('spark.sql.catalog.spark_catalog', 'org.apache.spark.sql.delta.catalog.DeltaCatalog')
        .config('spark.redaction.regex', '(?i)secret|password|token|access[.]key|jaas')
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel('WARN')
    schema = _catalog_schema()

    try:
        if args.mode == 'batch':
            raw_file = raw_path.rstrip('/') + '/venues_catalog.ndjson'
            df = spark.read.schema(schema).json(raw_file)
            _process_microbatch(df, 1, lake_base=lake_base, dataset_version=args.dataset_version)
        elif args.mode == 'stream':
            stream_df = spark.readStream.schema(schema).json(raw_path)
            writer = (
                stream_df.writeStream.foreachBatch(
                    lambda df, bid: _process_microbatch(df, bid, lake_base=lake_base, dataset_version=args.dataset_version)
                )
                .option('checkpointLocation', checkpoint_path)
                .trigger(availableNow=True)
            )
            query = writer.start()
            query.awaitTermination()
        else:
            reader = (
                spark.readStream.format('kafka')
                .option('kafka.bootstrap.servers', os.environ['VENUE_KAFKA_BOOTSTRAP'])
                .option('subscribe', os.environ['VENUE_KAFKA_TOPIC'])
                .option('startingOffsets', 'earliest')
                .option('maxOffsetsPerTrigger', 1000)
            )
            for key, value in spark_options('VENUE_KAFKA_OPTIONS_FILE').items():
                reader = reader.option(key, value)
            kafka_df = (
                reader.load()
                .select(f.from_json(f.col('value').cast('string'), schema).alias('data'))
                .select('data.*')
                .where(f.col('venue_id').isNotNull())
            )
            writer = (
                kafka_df.writeStream.foreachBatch(
                    lambda df, bid: _process_microbatch(df, bid, lake_base=lake_base, dataset_version=args.dataset_version)
                )
                .option('checkpointLocation', checkpoint_path)
                .trigger(availableNow=os.environ.get('SPARK_TRIGGER', 'microbatch') == 'available_now')
            )
            query = writer.start()
            query.awaitTermination()
    finally:
        # Release shared single-core batch slot immediately upon completion or failure.
        spark.stop()


if __name__ == '__main__':
    main()
