from ..errors import AnalyticsError
from ..events import identifier


class KafkaBroker:
    def __init__(self, producer, *, topic, ack_timeout=10):
        self.producer = producer
        self.topic = identifier(topic)
        if not 0 < ack_timeout < 30:
            raise AnalyticsError("INVALID_ACK_TIMEOUT")
        self.ack_timeout = ack_timeout

    @classmethod
    def connect(cls, *, bootstrap_servers, topic, username, password, ca_file):
        """Explicit network operation. Secrets are supplied by the host, never logged."""
        if not all((bootstrap_servers, username, password, ca_file)):
            raise AnalyticsError("KAFKA_TLS_CONFIGURATION_REQUIRED")
        from kafka import KafkaProducer

        producer = KafkaProducer(
            bootstrap_servers=bootstrap_servers,
            security_protocol="SASL_SSL",
            sasl_mechanism="PLAIN",
            sasl_plain_username=username,
            sasl_plain_password=password,
            ssl_cafile=ca_file,
            ssl_check_hostname=True,
            acks="all",
            retries=0,
            allow_auto_create_topics=False,
            request_timeout_ms=15_000,
            max_block_ms=20_000,
        )
        return cls(producer, topic=topic)

    def publish(self, *, key, value):
        # send() returning a future is not a broker ACK.
        return self.producer.send(self.topic, key=key, value=value).get(timeout=self.ack_timeout)

    def close(self):
        self.producer.close(timeout=self.ack_timeout)
