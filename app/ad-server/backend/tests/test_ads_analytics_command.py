from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from ads_analytics_v2.worker_cli import jaas_escape, sink_store
from ads_analytics_v2.worker_cli import main as worker_main
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from domains.ads.management.commands.run_ads_analytics import (
    Command,
    configuration,
    kafka_contract,
    mongo,
)

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
