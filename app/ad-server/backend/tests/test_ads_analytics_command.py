import ssl
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from ads_analytics_v2.worker_cli import jaas_escape, run_spark, sink_store, trusted_certificates
from ads_analytics_v2.worker_cli import main as worker_main
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from domains.ads.management.commands.run_ads_analytics import (
    Command,
    configuration,
    kafka_contract,
    mongo,
    next_delay,
)
from workers.ads_worker_health import healthy, write_health

CONFIG = dict(
    ADS_V2_ENABLED=True,
    ADS_MONGODB_URI="mongodb://source",
    ADS_MONGODB_DATABASE="legacy",
    ADS_V2_SOURCE_MONGODB_URI="mongodb://producer",
    ADS_V2_SOURCE_MONGODB_DATABASE="producer",
    ADS_V2_SINK_MONGODB_URI="mongodb://sink",
    ADS_V2_SINK_MONGODB_DATABASE="sink",
    ADS_V2_SOURCE_ID="catalog-v1",
    ADS_V2_MEDIA_ID="public",
    ADS_V2_INTERVAL_SECONDS=300,
)


@override_settings(**CONFIG)
class AnalyticsCommandTests(SimpleTestCase):
    @override_settings(ADS_V2_ENABLED=False)
    def test_disabled_refuses_before_clients_or_side_effects(self):
        with patch("domains.ads.management.commands.run_ads_analytics.mongo") as client:
            with self.assertRaises(CommandError):
                Command().handle(stage="collect", watch=False, page_size=1000, publish_limit=100)
            client.assert_not_called()

    @override_settings(ADS_V2_SINK_MONGODB_DATABASE="producer")
    def test_shared_source_sink_database_is_rejected(self):
        with self.assertRaises(CommandError):
            configuration("collect")

    def test_capture_needs_no_kafka_or_spark(self):
        configuration("collect")

    def test_incomplete_kafka_config_fails_before_connection(self):
        with self.assertRaises(CommandError):
            configuration("publish")

    def test_page_and_send_limits_are_bounded(self):
        with self.assertRaises(CommandError):
            Command().handle(stage="collect", watch=False, page_size=10001, publish_limit=100)

    def test_source_datetimes_are_decoded_with_timezone(self):
        with patch("domains.ads.management.commands.run_ads_analytics.MongoClient") as client:
            mongo("mongodb://example")
            self.assertTrue(client.call_args.kwargs["tz_aware"])

    def test_dead_listing_and_requeue_use_only_producer_store(self):
        module = "domains.ads.management.commands.run_ads_analytics."
        with patch(module + "mongo") as client, patch(module + "MongoStore") as stores:
            store = stores.return_value
            store.dead_letters.return_value = [
                {"event_id": "dead-event", "attempts": 8, "error_code": "BROKER_SEND_FAILED"}
            ]
            store.queue_counts.return_value = {"DEAD": 1}
            store.requeue_dead.return_value = 1
            command = Command(stdout=StringIO())
            command.handle(
                stage="dead", watch=False, page_size=1000, publish_limit=100, event_id=[]
            )
            store.prepare_indexes.assert_not_called()
            store.dead_letters.assert_called_once_with(limit=100)
            stores.assert_called_with(client.return_value[CONFIG["ADS_V2_SOURCE_MONGODB_DATABASE"]])
            command.handle(
                stage="requeue",
                watch=False,
                page_size=1000,
                publish_limit=100,
                event_id=["dead-event"],
            )
            self.assertEqual(store.requeue_dead.call_args.args[0], ["dead-event"])

    def test_requeue_requires_explicit_ids_and_refuses_watch(self):
        for ids, watch in (([], False), (["x"], True)):
            with self.assertRaises(CommandError):
                Command().handle(
                    stage="requeue", watch=watch, page_size=1000, publish_limit=100, event_id=ids
                )

    def test_jaas_single_backslash_and_quote_are_escaped(self):
        value = "a" + chr(92) + "b" + chr(34) + "c"
        expected = "a" + chr(92) * 2 + "b" + chr(92) + chr(34) + "c"
        self.assertEqual(jaas_escape(value), expected)

    def test_spark_sink_client_closed_on_batch_close_and_index_failure(self):
        env = {"ADS_V2_SINK_MONGODB_URI": "mongodb://sink", "ADS_V2_SINK_MONGODB_DATABASE": "sink"}
        with (
            patch("pymongo.MongoClient") as clients,
            patch("ads_analytics_v2.adapters.mongo.MongoStore") as stores,
        ):
            store = sink_store(env)
            self.assertTrue(clients.call_args.kwargs["tz_aware"])
            store.close()
            clients.return_value.close.assert_called_once()
            clients.return_value.close.reset_mock()
            stores.return_value.prepare_indexes.side_effect = RuntimeError("test")
            with self.assertRaises(RuntimeError):
                sink_store(env)
            clients.return_value.close.assert_called_once()

    def test_spark_worker_runtime_errors_do_not_expose_secrets(self):
        errors = StringIO()
        with (
            patch("ads_analytics_v2.worker_cli.validate_environment"),
            patch(
                "ads_analytics_v2.worker_cli.run_spark",
                side_effect=ValueError("mongodb://secret:password@host"),
            ),
            redirect_stderr(errors),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(worker_main([], {}), 2)
        self.assertNotIn("password", errors.getvalue())

    @override_settings(
        ADS_V2_KAFKA_TOPIC="ads-topic",
        ADS_V2_KAFKA_BOOTSTRAP_SERVERS="broker:9093",
        ADS_V2_KAFKA_USERNAME="u",
        ADS_V2_KAFKA_PASSWORD="p",
        ADS_V2_KAFKA_CA_FILE=__file__,
        ADS_V2_KAFKA_PARTITIONS=3,
        ADS_V2_KAFKA_REPLICATION_FACTOR=2,
    )
    def test_topic_contract_checks_existing_metadata_without_provisioning(self):
        module = MagicMock()
        client = module.KafkaAdminClient.return_value
        client.describe_topics.return_value = [
            {"topic": "ads-topic", "partitions": [{"replicas": [1, 2]} for _ in range(3)]}
        ]
        with patch.dict("sys.modules", {"kafka": MagicMock(), "kafka.admin": module}):
            kafka_contract()
            client.describe_topics.assert_called_once_with(["ads-topic"])
            client.create_topics.assert_not_called()
            client.close.assert_called_once()
            client.describe_topics.return_value[0]["partitions"].pop()
            with self.assertRaises(CommandError):
                kafka_contract()

    def test_collect_watch_drains_pages_before_interval_wait(self):
        module = "domains.ads.management.commands.run_ads_analytics."
        page = {
            "source_rows": 1000,
            "inserted": 1000,
            "sweep_complete": False,
            "source_completeness_verified": False,
        }
        with (
            patch(module + "mongo") as clients,
            patch(module + "MongoStore"),
            patch(
                "ads_analytics_v2.adapters.source_snapshot.capture_v1_page",
                side_effect=[page, page, {**page, "sweep_complete": True}, KeyboardInterrupt],
            ),
            patch(module + "time", wraps=time) as clock,
        ):
            clock.sleep.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                Command(stdout=StringIO()).loop(
                    "collect", {"watch": True, "page_size": 1000, "publish_limit": 100}
                )
            self.assertEqual([c.args[0] for c in clock.sleep.call_args_list], [0.01, 0.01, 300])
            clients.return_value.close.assert_called()

    def test_publish_watch_drains_backlog_and_polls_delayed_retry(self):
        module = "domains.ads.management.commands.run_ads_analytics."
        with (
            patch(module + "mongo"),
            patch(module + "MongoStore") as stores,
            patch("ads_analytics_v2.adapters.kafka.KafkaBroker.connect"),
            patch(
                "ads_analytics_v2.transport.publish_once",
                side_effect=[
                    {"claimed": 100},
                    {"claimed": 100},
                    {"claimed": 0},
                    {"claimed": 0},
                    KeyboardInterrupt,
                ],
            ),
            patch(module + "time", wraps=time) as clock,
        ):
            stores.return_value.queue_counts.side_effect = [{"PENDING": 1}, {"SENT": 200}]
            clock.sleep.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                Command(stdout=StringIO()).loop(
                    "publish", {"watch": True, "page_size": 1000, "publish_limit": 100}
                )
            self.assertEqual([c.args[0] for c in clock.sleep.call_args_list], [0.01, 0.01, 1, 300])

    def test_abandoned_lease_uses_short_poll_not_full_interval(self):
        self.assertEqual(
            next_delay("publish", {"claimed": 0}, {"SENDING": 1}, interval=300, publish_limit=100),
            1,
        )

    def test_public_ca_parser_preserves_multiline_pem_and_rejects_private_key(self):
        roots = ssl.create_default_context().get_ca_certs(binary_form=True)
        if not roots:
            self.skipTest("호스트에 공개 CA fixture가 없습니다.")
        pem = ssl.DER_cert_to_PEM_cert(roots[0])
        with patch("ads_analytics_v2.worker_cli.Path") as paths:
            paths.return_value.is_file.return_value = True
            paths.return_value.stat.return_value.st_size = len(pem)
            paths.return_value.read_text.return_value = pem
            self.assertEqual(trusted_certificates("public-ca.pem"), pem)
            paths.return_value.read_text.return_value = pem + "\n-----BEGIN " + "PRIVATE KEY-----\n"
            with self.assertRaises(ValueError):
                trusted_certificates("private-material.pem")
            paths.return_value.read_text.return_value = "not a certificate"
            with self.assertRaises(ValueError):
                trusted_certificates("invalid.pem")

    def test_spark_consumer_receives_pem_contents_without_driver_file_path(self):
        pem = "-----BEGIN CERTIFICATE-----\npublic-ca-multiline\n-----END CERTIFICATE-----\n"
        env = {
            "ADS_V2_KAFKA_CA_FILE": "/driver-only/ca.pem",
            "ADS_V2_SPARK_MASTER": "spark://master:7077",
            "ADS_V2_SPARK_JARS": "/driver/kafka.jar,/driver/s3a.jar",
            "ADS_V2_KAFKA_USERNAME": "u",
            "ADS_V2_KAFKA_PASSWORD": "p",
            "ADS_V2_KAFKA_BOOTSTRAP_SERVERS": "broker:9093",
            "ADS_V2_KAFKA_TOPIC": "topic",
            "ADS_V2_CHECKPOINT": "s3a://checkpoints/unique-job",
        }
        fake_sql = MagicMock()
        with (
            patch.dict("sys.modules", {"pyspark": MagicMock(), "pyspark.sql": fake_sql}),
            patch("ads_analytics_v2.worker_cli.verify_topic"),
            patch("ads_analytics_v2.worker_cli.trusted_certificates", return_value=pem),
            patch("ads_analytics_v2.adapters.spark.stream_from_kafka") as stream,
            redirect_stdout(StringIO()),
        ):
            run_spark(env)
            security = stream.call_args.kwargs["security_options"]
            self.assertEqual(security["kafka.ssl.truststore.certificates"], pem)
            self.assertEqual(security["kafka.ssl.truststore.type"], "PEM")
            self.assertNotIn("kafka.ssl.truststore.location", security)
            stream.return_value.awaitTermination.assert_called_once()
            stream.return_value.stop.assert_called_once()


class PolicyWorkerHealthTests(SimpleTestCase):
    def test_health_requires_recent_success_and_recent_heartbeat(self):
        now = 1800000000
        state = {
            "written_at": now,
            "jobs": {"policy": {"status": "OK", "last_succeeded_at": "2027-01-15T08:00:00Z"}},
        }
        from datetime import UTC, datetime

        state["jobs"]["policy"]["last_succeeded_at"] = datetime.fromtimestamp(
            now - 30, UTC
        ).isoformat()
        self.assertTrue(healthy(state, now=now))
        self.assertFalse(healthy(state, now=now + 66))
        state["written_at"] = now + 400
        self.assertFalse(healthy(state, now=now + 400))
        state["written_at"] = now
        state["jobs"]["policy"]["status"] = "ERROR"
        self.assertFalse(healthy(state, now=now))

    def test_health_writer_is_opt_in_and_atomically_replaces_snapshot(self):
        with patch("workers.ads_worker_health.Path") as paths:
            write_health({}, filename="")
            paths.assert_not_called()
            write_health(
                {"policy": {"status": "RUNNING"}}, filename="/tmp/ads-policy-health.json", now=10
            )
            temporary = paths.return_value.with_name.return_value
            temporary.write_text.assert_called_once()
            temporary.replace.assert_called_once_with(paths.return_value)
