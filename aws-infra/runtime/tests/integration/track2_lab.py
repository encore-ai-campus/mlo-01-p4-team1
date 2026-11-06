"""Shared, isolated Elastic test setup; never touches an existing project."""

import json
import os
import time

from mlo_runtime.catalog import Elastic
from mlo_runtime.lab import Lab, certificates
from mlo_runtime.operations import UnsafeTransition


def service(lab, image, **settings):
    return {
        "image": lab.image(image),
        "platform": os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64"),
        "mem_limit": "1536m",
        "cpus": 1,
        "pids_limit": 512,
        "labels": {"mlo.role": "B", "mlo.run": lab.project},
        **settings,
    }


def elastic_lab(name):
    lab = Lab(name)
    lab.service = "elasticsearch"
    certificates(lab.folder, ["elasticsearch", "logstash", "kibana"])
    # The ephemeral certificate has both TLS client/server EKUs. Production
    # consumes separately provisioned per-agent client identities.
    (lab.folder / "client.crt").write_bytes((lab.folder / "server.crt").read_bytes())
    (lab.folder / "client.key").write_bytes((lab.folder / "server.key").read_bytes())
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(
        (lab.folder / "server.key").read_bytes(), password=None
    )
    (lab.folder / "server-pkcs8.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    config = {
        "services": {
            "elasticsearch": service(
                lab,
                "elasticsearch",
                ports=["127.0.0.1::9200"],
                environment={
                    "discovery.type": "single-node",
                    "ELASTIC_PASSWORD": "fixture",
                    "ES_JAVA_OPTS": "-Xms512m -Xmx512m",
                    "xpack.security.enabled": "true",
                    "xpack.security.http.ssl.enabled": "true",
                    "xpack.security.http.ssl.key": "/usr/share/elasticsearch/config/lab/server.key",
                    "xpack.security.http.ssl.certificate": "/usr/share/elasticsearch/config/lab/server.crt",
                },
                volumes=[
                    "data:/usr/share/elasticsearch/data",
                    f"{lab.folder}:/usr/share/elasticsearch/config/lab:ro",
                ],
            )
        },
        "volumes": {"data": {}},
    }
    lab.compose_path.write_text(json.dumps(config))
    return lab, config


def start_es(lab):
    lab.compose("up", "-d", "elasticsearch", timeout=180)
    lab.ready(
        [
            "curl",
            "--fail",
            "--silent",
            "--cacert",
            "/usr/share/elasticsearch/config/lab/ca.crt",
            "-u",
            "elastic:fixture",
            "https://localhost:9200/_cluster/health?wait_for_status=yellow",
        ]
    )
    endpoint = lab.compose("port", "elasticsearch", "9200").stdout.decode().strip()
    return Elastic(
        "https://" + endpoint, str(lab.folder / "ca.crt"), "elastic", "fixture"
    )


def wait_for(predicate, *, seconds=150):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            # Invalid requests/auth are not startup races. Do not obscure them
            # behind a progress timeout or relax production TLS/RBAC to pass.
            if isinstance(exc, UnsafeTransition) and str(exc) in {
                "ES request rejected: HTTP " + str(code)
                for code in (400, 401, 403, 405)
            }:
                raise AssertionError(
                    "isolated Track2 request rejected; not a transient readiness error"
                ) from exc
        time.sleep(1)
    raise AssertionError(
        "isolated Track2 readiness/progress deadline exceeded"
    ) from last_error
