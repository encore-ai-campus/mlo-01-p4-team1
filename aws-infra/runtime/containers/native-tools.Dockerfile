# Test tooling only. Production Kafka/Spark/ES run directly under host systemd.
FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
RUN apt-get update -qq && apt-get install -y --no-install-recommends systemd=255.4-1ubuntu8.18 python3=3.12.3-0ubuntu2.1 curl=8.5.0-2ubuntu10.16 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 2000 mlo && useradd -u 2000 -g mlo -M -s /usr/sbin/nologin mlo
CMD ["sleep", "infinity"]
