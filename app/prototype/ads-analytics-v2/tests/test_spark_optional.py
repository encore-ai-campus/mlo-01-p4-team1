import importlib.util
import unittest

from ads_analytics_v2.reporting import report
from tests.helpers import CUTOFF, SETTLED, event


@unittest.skipUnless(importlib.util.find_spec("pyspark"), "PySpark/Java runtime not installed: real Spark unverified")
class SparkParityTests(unittest.TestCase):
    def test_native_spark_join_and_groupby_match_reference(self):
        from pyspark.sql import SparkSession
        from ads_analytics_v2.adapters.spark import report_with_spark
        spark = SparkSession.builder.master("local[2]").appName("ads-v2-isolated-test").getOrCreate()
        try:
            events = [event(occurred="2026-11-05T14:59:59Z"),
                      event("c", kind="CLICK", occurred="2026-11-05T15:00:01Z"),
                      event("early", delivery="de", kind="CLICK"),
                      event("ie", delivery="de", occurred="2026-11-06T00:00:01Z"),
                      event("orphan", delivery="do", kind="CLICK")]
            expected = report(events, cutoff=CUTOFF, settled_through=SETTLED)
            actual = report_with_spark(spark, events, cutoff=CUTOFF, settled_through=SETTLED)
            self.assertEqual(actual, expected)
        finally:
            spark.stop()
