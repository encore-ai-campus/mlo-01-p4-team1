"""Pure inventory adapter and per-node runtime rendering. No AWS clients."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import fcntl
import shutil
import sys
import tempfile
import uuid
import yaml

from .contracts import (
    RUNTIME,
    ContractError,
    load_inventory,
    node_services,
    require,
    validate_runtime_plan,
)

from .queries import QUERIES
TIMERS = {
    "control": {
        "feedback-analytics-batch": "*:0/15",
        "raw-reconcile": "*:0/5",
        "behavior-export": "*-*-* *:*:00",
    },
    "postgresql": {"postgresql-backup": "hourly"},
    "mongodb": {"mongodb-backup": "hourly"},
}
ROLE_SECRETS = {
    "control": [
        "postgresql",
        "mongodb",
        "kafka",
        "elasticsearch",
        "zookeeper",
        "llm_service",
        "ghcr_read",
    ],
    "gateway": ["postgresql", "kafka", "llm_service", "ghcr_read"],
    "spark": [
        "postgresql",
        "mongodb",
        "kafka",
        "elasticsearch",
        "zookeeper",
        "ghcr_read",
    ],
    "advertising": ["mongodb", "ghcr_read"],
    "postgresql": ["postgresql", "ghcr_read"],
    "mongodb": ["mongodb", "ghcr_read"],
    "kafka": ["kafka", "ghcr_read"],
    "elasticsearch": ["elasticsearch", "ghcr_read"],
}


def stable_id(node):
    """IDs belong to approved named nodes, never their inventory array position."""
    prefix = {"kafka": "kafka", "spark": "spark"}.get(node["role"])
    require(prefix is not None, "role has no cluster identity")
    match = re.fullmatch(prefix + r"-0([123])", node["name"])
    require(match is not None, "unknown cluster node identity")
    return int(match.group(1))


def encode(value):
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


def no_proxy(inventory):
    # Explicit hosts as well as CIDRs: not every HTTP client implements CIDRs.
    return ",".join(
        dict.fromkeys(
            [
                "localhost",
                "127.0.0.1",
                "::1",
                "169.254.169.254",
                "169.254.170.2",
                ".internal",
                ".ts.net",
                "100.64.0.0/10",
                "ssm.ap-northeast-2.amazonaws.com",
                "ssmmessages.ap-northeast-2.amazonaws.com",
                "s3.ap-northeast-2.amazonaws.com",
                ".s3.ap-northeast-2.amazonaws.com",
                *[a["cidr"] for a in inventory["accounts"].values()],
                *[n["private_ip"] for n in inventory["nodes"]],
                *[n["name"] for n in inventory["nodes"]],
            ]
        )
    )


def adapter(inventory):
    children = {}
    for node in inventory["nodes"]:
        children.setdefault("runtime_" + node["role"], {"hosts": {}})["hosts"][
            node["name"]
        ] = {
            "ansible_host": node["instance_id"],
            "ansible_connection": "amazon.aws.aws_ssm",
            "ansible_aws_ssm_profile": "p4-controller-" + node["account"],
            "ansible_aws_ssm_region": inventory["region"],
            "ansible_aws_ssm_bucket_name": inventory["storage"]["transfer_bucket_ref"],
            "ansible_aws_ssm_s3_addressing_style": "virtual",
            "ansible_aws_ssm_bucket_sse_mode": "AES256",
            "ansible_python_interpreter": "/usr/bin/python3",
            "runtime_node": node,
        }
    return {
        "all": {
            "vars": {
                "runtime_example_only": inventory["example_only"],
                "runtime_environment": inventory["environment"],
                "runtime_generation": inventory["generation"],
            },
            "children": children,
        }
    }


def spark_defaults(inventory, node):
    zk = ",".join(
        f"{n['private_ip']}:2182" for n in sorted(inventory["nodes"], key=lambda n: n["name"]) if n["role"] == "spark"
    )
    return (
        "spark.authenticate true\nspark.authenticate.secret.file /run/mlo/secrets/spark-auth\n"
        "spark.ssl.rpc.enabled true\nspark.network.crypto.enabled false\nspark.network.crypto.saslFallback false\n"
        "spark.network.crypto.authEngineVersion 2\nspark.port.maxRetries 0\n"
        "spark.deploy.recoveryMode ZOOKEEPER\n"
        f"spark.deploy.zookeeper.url {zk}\n"
        f"spark.deploy.zookeeper.dir /mlo/{inventory['environment']}/{inventory['generation']}\n"
        "spark.ssl.enabled true\nspark.ssl.keyStore /run/mlo/secrets/spark-keystore.p12\n"
        "spark.ssl.trustStore /run/mlo/secrets/cluster-truststore.p12\n"
        "spark.hadoop.fs.s3a.aws.credentials.provider software.amazon.awssdk.auth.credentials.InstanceProfileCredentialsProvider\n"
        "spark.hadoop.fs.s3a.endpoint s3.ap-northeast-2.amazonaws.com\n"
        "spark.hadoop.fs.s3a.endpoint.region ap-northeast-2\n"
        "spark.hadoop.fs.s3a.path.style.access false\n"
        "spark.sql.extensions io.delta.sql.DeltaSparkSessionExtension\n"
        "spark.sql.catalog.spark_catalog org.apache.spark.sql.delta.catalog.DeltaCatalog\n"
    )


def haproxy(inventory):
    backends = "\n".join(
        f"  server {n['name']} {n['private_ip']}:3128 check inter 2s fall 2 rise 2"
        for n in inventory["nodes"]
        if n["role"] == "gateway"
    )
    return (
        "global\n  maxconn 1024\ndefaults\n  mode tcp\n  timeout connect 3s\n"
        "  timeout client 60s\n  timeout server 60s\nfrontend host_proxy\n"
        "  bind 127.0.0.1:3129\n  default_backend gateways\nbackend gateways\n"
        "  balance roundrobin\n" + backends + "\n"
    )


def systemd_units(node, inventory):
    units = {}
    root = "/srv/mlo/" + inventory["generation"]
    if node["role"] == "control":
        units["mlo-supervisor.service"] = (
            "[Unit]\nDescription=MLO active execution group supervisor\n"
            "After=docker.service network-online.target\nRequires=docker.service\nRequiresMountsFor=" + root + "\n"
            "[Service]\nType=simple\nUser=root\nUMask=0077\n"
            "Environment=PYTHONPATH=/opt/mlo/current/aws-infra/runtime/src\n"
            "ExecStart=/usr/bin/python3 -m mlo_runtime.supervisor --binding /etc/mlo/supervisor.json\n"
            "TimeoutStopSec=90s\nRestart=on-failure\nRestartSec=5s\nNoNewPrivileges=true\n"
            "ProtectSystem=strict\nReadWritePaths=/run/mlo /var/lib/mlo " + root + "\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
    for job, schedule in TIMERS.get(node["role"], {}).items():
        units["mlo-" + job + ".service"] = (
            "[Unit]\nDescription=MLO "
            + job
            + "\nRequiresMountsFor="
            + root
            + "\nAfter=network-online.target\n"
            "[Service]\nType=oneshot\nUser=root\nUMask=0077\n"
            "Environment=PYTHONPATH=/opt/mlo/current/aws-infra/runtime/src\n"
            "ExecStart=/usr/bin/python3 -m mlo_runtime.jobs --job " + job + "\n"
            "TimeoutStartSec=14min\nNoNewPrivileges=true\nProtectSystem=strict\n"
            "ReadWritePaths=/run/mlo " + root + "\nPrivateTmp=true\n"
        )
        units["mlo-" + job + ".timer"] = (
            "[Unit]\nDescription=MLO timer "
            + job
            + "\n[Timer]\nOnCalendar="
            + schedule
            + "\nPersistent=true\nAccuracySec=1s\n[Install]\nWantedBy=timers.target\n"
        )
    if node["role"] in {"postgresql", "mongodb"}:
        job = node["role"] + "-backup"
        units["mlo-backup-before-shutdown.service"] = (
            "[Unit]\nDescription=MLO shutdown backup gate\nAfter=docker.service network-online.target\n"
            "Requires=docker.service\nRequiresMountsFor=" + root + "\n"
            "[Service]\nType=oneshot\nRemainAfterExit=true\n"
            "Environment=PYTHONPATH=/opt/mlo/current/aws-infra/runtime/src\n"
            "ExecStart=/usr/bin/python3 -m mlo_runtime.jobs --validate-only --job "
            + job
            + "\n"
            "ExecStop=/usr/bin/python3 -m mlo_runtime.jobs --job " + job + "\n"
            "TimeoutStopSec=15min\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n"
        )
        units["mlo-stateful-stop.service"] = (
            "[Unit]\nDescription=MLO explicitly requested verified stateful stop\n"
            "After=docker.service network-online.target\nRequiresMountsFor=" + root + "\n"
            "[Service]\nType=oneshot\nUMask=0077\n"
            "Environment=PYTHONPATH=/opt/mlo/current/aws-infra/runtime/src\n"
            "ExecStart=/usr/bin/python3 -m mlo_runtime.stateful --binding /etc/mlo/stateful-stop.json --confirm-planned-stop " + node["name"] + "\n"
            "TimeoutStartSec=30min\n"
        )
    return units


def service_configs(node, inventory):
    ip, role = node["private_ip"], node["role"]
    grouped = lambda role: sorted(
        (n for n in inventory["nodes"] if n["role"] == role), key=lambda n: n["name"]
    )
    files = {}
    if role in {"control", "advertising"}:
        kind = "web" if role == "control" else "advertising"
        files["service.json"] = encode({"kind": kind,
                                         "bind": f"{ip}:{8000 if kind == 'web' else 8001}",
                                         "secret_env": "/run/mlo/secrets/app-env.json"})
    if role == "kafka":
        voters = ",".join(
            f"{stable_id(n)}@{n['private_ip']}:9094" for n in grouped("kafka")
        )
        node_id = stable_id(node)
        files["server.properties"] = (
            f"process.roles=broker,controller\nnode.id={node_id}\ncontroller.quorum.voters={voters}\n"
            f"listeners=CLIENT://{ip}:9093,CONTROLLER://{ip}:9094\nadvertised.listeners=CLIENT://{ip}:9093\n"
            "listener.security.protocol.map=CLIENT:SSL,CONTROLLER:SSL\ncontroller.listener.names=CONTROLLER\n"
            "inter.broker.listener.name=CLIENT\nssl.client.auth=required\n"
            "ssl.keystore.type=PKCS12\nssl.keystore.location=/run/mlo/secrets/kafka-keystore.p12\n"
            "ssl.truststore.type=PKCS12\nssl.truststore.location=/run/mlo/secrets/cluster-truststore.p12\n"
            "config.providers=file\nconfig.providers.file.class=org.apache.kafka.common.config.provider.FileConfigProvider\n"
            "ssl.keystore.password=${file:/run/mlo/secrets/kafka-passwords.properties:keystore}\n"
            "ssl.truststore.password=${file:/run/mlo/secrets/kafka-passwords.properties:truststore}\n"
            "authorizer.class.name=org.apache.kafka.metadata.authorizer.StandardAuthorizer\n"
            "allow.everyone.if.no.acl.found=false\n"
            "log.dirs=/var/lib/kafka/data\nnum.partitions=3\ndefault.replication.factor=3\n"
            "min.insync.replicas=2\noffsets.topic.replication.factor=3\n"
            "transaction.state.log.replication.factor=3\ntransaction.state.log.min.isr=2\n"
            "log.retention.hours=168\nauto.create.topics.enable=false\n"
        )
    elif role == "postgresql":
        files["postgresql.conf"] = (
            f"listen_addresses='{ip}'\nport=5432\nssl=on\nssl_min_protocol_version='TLSv1.2'\n"
            "ssl_cert_file='/run/mlo/secrets/server.crt'\nssl_key_file='/run/mlo/secrets/server.key'\n"
            "password_encryption='scram-sha-256'\n"
        )
        clients = [
            n
            for n in inventory["nodes"]
            if n["role"] in {"control", "spark", "gateway"}
        ]
        files["pg_hba.conf"] = "local all all peer\n" + "".join(
            f"hostssl all all {n['private_ip']}/32 scram-sha-256\n" for n in clients
        )
    elif role == "mongodb":
        files["mongod.yaml"] = yaml.safe_dump(
            {
                "storage": {"dbPath": "/data/db"},
                "net": {
                    "bindIp": ip,
                    "port": 27017,
                    "tls": {
                        "mode": "requireTLS",
                        "certificateKeyFile": "/run/mlo/secrets/server.pem",
                        "CAFile": "/run/mlo/secrets/ca.crt",
                    },
                },
                "replication": {"replSetName": "ads-rs"},
                "security": {"authorization": "enabled", "keyFile": "/run/mlo/secrets/mongodb-keyfile"},
            }
        )
    elif role == "elasticsearch":
        files["elasticsearch.yml"] = yaml.safe_dump(
            {
                "cluster.name": "mlo-" + inventory["generation"],
                "node.name": node["name"],
                "network.host": ip,
                "http.port": 9200,
                "transport.port": 9300,
                "discovery.seed_hosts": [
                    n["private_ip"] for n in grouped("elasticsearch")
                ],
                "xpack.security.enabled": True,
                "xpack.security.http.ssl.enabled": True,
                "xpack.security.http.ssl.key": "secrets/server.key",
                "xpack.security.http.ssl.certificate": "secrets/server.crt",
                "xpack.security.transport.ssl.enabled": True,
                "xpack.security.transport.ssl.verification_mode": "full",
                "xpack.security.transport.ssl.key": "secrets/server.key",
                "xpack.security.transport.ssl.certificate": "secrets/server.crt",
                "xpack.security.transport.ssl.certificate_authorities": [
                    "secrets/ca.crt"
                ],
            }
        )
    elif role == "spark":
        files["spark-defaults.conf"] = spark_defaults(inventory, node)
        sid = stable_id(node)
        files["myid"] = str(sid) + "\n"
        files["zoo.cfg"] = (
            "tickTime=2000\ninitLimit=10\nsyncLimit=5\ndataDir=/data/zookeeper\n"
            "secureClientPort=2182\nserverCnxnFactory=org.apache.zookeeper.server.NettyServerCnxnFactory\n"
            "sslQuorum=true\nssl.quorum.clientAuth=need\nssl.clientAuth=need\n"
            "ssl.keyStore.location=/run/mlo/secrets/zookeeper-keystore.p12\n"
            "ssl.trustStore.location=/run/mlo/secrets/cluster-truststore.p12\n"
            "ssl.keyStore.passwordPath=/run/mlo/secrets/keystore-password\n"
            "ssl.trustStore.passwordPath=/run/mlo/secrets/truststore-password\n"
            "ssl.quorum.keyStore.location=/run/mlo/secrets/zookeeper-keystore.p12\n"
            "ssl.quorum.trustStore.location=/run/mlo/secrets/cluster-truststore.p12\n"
            "ssl.quorum.keyStore.passwordPath=/run/mlo/secrets/keystore-password\n"
            "ssl.quorum.trustStore.passwordPath=/run/mlo/secrets/truststore-password\n"
            + "".join(
                f"server.{stable_id(n)}={n['private_ip']}:2888:3888\n"
                for n in grouped("spark")
            )
        )
    elif role == "gateway":
        cidrs = " ".join(a["cidr"] for a in inventory["accounts"].values())
        files["squid.conf"] = (
            f"http_port {ip}:3128\nacl runtime src {cidrs}\n"
            "acl SSL_ports port 443\nacl Safe_ports port 80 443\nacl CONNECT method CONNECT\n"
            "http_access deny !Safe_ports\nhttp_access deny CONNECT !SSL_ports\n"
            "http_access allow runtime\nhttp_access deny all\ncache deny all\n"
            "access_log stdio:/dev/stdout\ncache_log /dev/stderr\npid_filename /tmp/squid.pid\n"
        )
    return files


def compose(node, inventory):
    role = node["role"]
    proxy = (
        {}
        if role == "gateway"
        else {
            k: "http://127.0.0.1:3129"
            for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]
        }
    )
    proxy.update({"NO_PROXY": no_proxy(inventory), "no_proxy": no_proxy(inventory)})

    def svc(image_key, memory, command, mounts, health):
        locks = json.loads((RUNTIME / "images.lock.json").read_text())["images"]
        image = (
            locks[image_key]["ref"] + "@" + locks[image_key]["digest"]
            if image_key in locks
            else "${MLO_IMAGE_"
            + image_key.upper()
            + ":?verified digest binding required}"
        )
        return {
            "image": image,
            "platform": "linux/amd64",
            "network_mode": "host",
            "restart": "unless-stopped",
            "init": True,
            "pids_limit": 512,
            "mem_limit": memory,
            "cpus": 1.0,
            "security_opt": ["no-new-privileges:true"],
            "environment": proxy.copy(),
            "logging": {
                "driver": "local",
                "options": {"max-size": "10m", "max-file": "3"},
            },
            "volumes": mounts + ["/run/mlo/secrets:/run/mlo/secrets:ro"],
            "command": command,
            "healthcheck": {
                "test": ["CMD-SHELL", health],
                "interval": "15s",
                "timeout": "5s",
                "retries": 5,
                "start_period": "60s",
            },
        }

    config = "/etc/mlo/current/config"
    data = "/srv/mlo/" + inventory["generation"]
    services = {}
    if role != "gateway":
        services["host-proxy"] = svc(
            "haproxy",
            "128m",
            ["haproxy", "-W", "-db", "-f", "/etc/haproxy/haproxy.cfg"],
            [f"{config}/haproxy.cfg:/etc/haproxy/haproxy.cfg:ro"],
            "haproxy -c -f /etc/haproxy/haproxy.cfg",
        )
        services["host-proxy"]["environment"] = {}
    if role == "postgresql":
        services[role] = svc(
            role,
            "2g",
            [
                "postgres",
                "-c",
                "config_file=/etc/postgresql/postgresql.conf",
                "-c",
                "hba_file=/etc/postgresql/pg_hba.conf",
            ],
            [
                f"{data}/postgresql:/var/lib/postgresql/data",
                f"{config}/postgresql.conf:/etc/postgresql/postgresql.conf:ro",
                f"{config}/pg_hba.conf:/etc/postgresql/pg_hba.conf:ro",
            ],
            "pg_isready -h " + node["private_ip"] + " -U mlo",
        )
        services[role]["environment"].update(
            {
                "POSTGRES_USER": "mlo",
                "POSTGRES_PASSWORD_FILE": "/run/mlo/secrets/postgresql-password",
            }
        )
    elif role == "mongodb":
        services[role] = svc(
            role,
            "2g",
            ["mongod", "--config", "/etc/mongod.yaml"],
            [f"{data}/mongodb:/data/db", f"{config}/mongod.yaml:/etc/mongod.yaml:ro"],
            "mongosh --quiet --tls --tlsCAFile /run/mlo/secrets/ca.crt --host "
            + node["private_ip"]
            + " --eval 'quit(db.runCommand({ping:1}).ok ? 0 : 1)'",
        )
    elif role == "kafka":
        services[role] = svc(
            role,
            "2g",
            ["/opt/kafka/bin/kafka-server-start.sh", "/etc/kafka/server.properties"],
            [
                f"{data}/kafka:/var/lib/kafka/data",
                f"{config}/server.properties:/etc/kafka/server.properties:ro",
            ],
            "/opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server "
            + node["private_ip"]
            + ":9093 --command-config /run/mlo/secrets/kafka-client.properties",
        )
        # Bypass image's automatic storage formatting. Initial format is a separate inspected procedure.
        services[role]["entrypoint"] = services[role].pop("command")
        services[role]["environment"]["KAFKA_HEAP_OPTS"] = "-Xms768m -Xmx768m"
    elif role == "spark":
        services["zookeeper"] = svc(
            "zookeeper",
            "512m",
            ["zkServer.sh", "start-foreground"],
            [f"{data}/zookeeper:/data/zookeeper", f"{config}/zoo.cfg:/conf/zoo.cfg:ro"],
            "zkServer.sh status /conf/zoo.cfg",
        )
        # The stock image entrypoint may generate a new myid from environment defaults.
        # Installation writes/validates /data/zookeeper/myid before this direct command.
        services["zookeeper"]["entrypoint"] = services["zookeeper"].pop("command")
        for component, port in [("master", 7077), ("worker", 7078)]:
            cmd = [
                "/opt/spark/bin/spark-class",
                "org.apache.spark.deploy." + component + "." + component.title(),
                "--host",
                node["private_ip"],
                "--port",
                str(port),
                "--webui-port",
                "8080" if component == "master" else "8081",
            ]
            if component == "worker":
                cmd += [
                    "--cores",
                    "2",
                    "--memory",
                    "3g",
                    "--work-dir",
                    "/data/spark",
                    "spark://"
                    + ",".join(
                        n["private_ip"] + ":7077"
                        for n in sorted(inventory["nodes"], key=lambda n: n["name"])
                        if n["role"] == "spark"
                    ),
                ]
            services["spark-" + component] = svc(
                "spark",
                "1g" if component == "master" else "5g",
                cmd,
                [
                    f"{data}/spark:/data/spark",
                    f"{config}/spark-defaults.conf:/opt/spark/conf/spark-defaults.conf:ro",
                ],
                f"curl --fail --cacert /run/mlo/secrets/ca.crt --cert /run/mlo/secrets/client.crt --key /run/mlo/secrets/client.key https://{node['private_ip']}:{8080 if component == 'master' else 8081}/json/",
            )
            services["spark-" + component]["entrypoint"] = services[
                "spark-" + component
            ].pop("command")
    elif role == "elasticsearch":
        services[role] = svc(
            role,
            "5g",
            ["eswrapper"],
            [
                f"{data}/elasticsearch:/usr/share/elasticsearch/data",
                f"{config}/elasticsearch.yml:/usr/share/elasticsearch/config/elasticsearch.yml:ro",
                "/run/mlo/secrets:/usr/share/elasticsearch/config/secrets:ro",
            ],
            "curl --fail --cacert /run/mlo/secrets/ca.crt --netrc-file /run/mlo/secrets/es-netrc https://"
            + node["private_ip"]
            + ":9200/_cluster/health?wait_for_status=yellow",
        )
        services[role]["environment"]["ES_JAVA_OPTS"] = "-Xms2g -Xmx2g"
    else:
        keys = {
            "control": ["web"],
            "advertising": ["advertising"],
            "gateway": ["forward-proxy", "llm-relay"],
        }[role]
        if node["name"] == "control-b":
            keys += ["kibana", "log-ingest"]
        for key in keys:
            services[key] = svc(
                key.replace("-", "_"),
                "512m",
                ["/opt/mlo/bin/serve", "--config", "/etc/mlo/service.json"],
                [f"{data}/spool:/data/spool"],
                "test -x /opt/mlo/bin/readiness && /opt/mlo/bin/readiness",
            )
            if key in {"web", "advertising"}:
                services[key]["volumes"].append(f"{config}/service.json:/etc/mlo/service.json:ro")
        if role == "gateway":
            services["forward-proxy"] = svc(
                "squid",
                "256m",
                ["squid", "-N", "-f", "/etc/squid/squid.conf"],
                [f"{config}/squid.conf:/etc/squid/squid.conf:ro"],
                "squid -k check -f /etc/squid/squid.conf",
            )
            services["llm-relay"]["command"] = ["python3", "-m", "mlo_runtime.relay", "--binding", "/etc/mlo/relay.json"]
            services["llm-relay"]["volumes"] += [
                "/opt/mlo/current:/opt/mlo/runtime:ro",
                "/etc/mlo/relay.json:/etc/mlo/relay.json:ro",
            ]
            services["llm-relay"]["environment"]["PYTHONPATH"] = "/opt/mlo/runtime/aws-infra/runtime/src"
            services["llm-relay"]["environment"]["PYTHONDONTWRITEBYTECODE"] = "1"
            services["llm-relay"]["healthcheck"]["test"] = ["CMD", "python3", "-m", "mlo_runtime.relay", "--binding", "/etc/mlo/relay.json", "--check"]
    return {"name": "mlo-runtime-" + node["name"], "services": services}


def build(inventory, specification):
    plan = {
        k: inventory[k]
        for k in ["schema_version", "environment", "generation", "example_only"]
    }
    plan.update(ready_for_aws=False, node_plans=[])
    details = {
        **{k: plan[k] for k in plan if k != "node_plans"},
        "inventory_sha256": hashlib.sha256(encode(inventory).encode()).hexdigest(),
        "unresolved": [
            "AMI + mounted filesystem UUIDs",
            "image digests + source locks",
            "private secret/certificate bindings",
            "SSM Document version/hash",
            "actual app entrypoints/health/migration compatibility",
            "tailnet identities + model API",
            "Spark/Delta connector jars + TLS credentials",
            "KRaft cluster identity + ACL bootstrap",
            "ES initial cluster bootstrap + MongoDB initial user",
        ],
        "nodes": {},
    }
    for node in inventory["nodes"]:
        selected = node_services(node, specification["services"])
        plan["node_plans"].append(
            {
                "node": node["name"],
                "role": node["role"],
                "services": selected,
                "destructive": False,
            }
        )
        timers = TIMERS.get(node["role"], {})
        details["nodes"][node["name"]] = {
            "listeners": {name: specification["services"][name] for name in selected},
            "persistent_root": "/srv/mlo/" + inventory["generation"],
            "secret_refs": {
                k: inventory["secret_refs"][k] for k in ROLE_SECRETS[node["role"]]
            },
            "timers": timers,
            "backup_before_shutdown": node["role"] in {"postgresql", "mongodb"},
            "spark_ports": QUERIES if node["role"] == "control" else {},
            "spark_executor_ports": (
                {q: 19006 + i for i, q in enumerate(QUERIES)}
                if node["role"] == "control"
                else {}
            ),
            "checkpoint_prefixes": (
                {
                    q: specification["s3_paths"]["checkpoint"].format(
                        environment=inventory["environment"],
                        generation=inventory["generation"],
                        query=q,
                    )
                    for q in QUERIES
                }
                if node["role"] == "control"
                else {}
            ),
            "s3_paths": specification["s3_paths"],
            "no_proxy": no_proxy(inventory),
            "weather_collector": (
                ("active-manual" if node["name"] == "gateway-a" else "standby-manual")
                if node["role"] == "gateway"
                else None
            ),
            "platform": "linux/amd64",
            "cluster_identity": (
                {"node_id": stable_id(node), "kind": node["role"],
                 "cluster_id": "accepted-host-binding-required" if node["role"] == "kafka" else None}
                if node["role"] in {"kafka", "spark"} else None
            ),
            "imds": "host networking; IMDSv2 token; bypass proxy; instance profile; no static AWS credentials",
        }
    validate_runtime_plan(plan, inventory, specification)
    return plan, details


def _render_staged(inventory_path, output, bundle=None, expected_generation=None):
    inventory, specification = load_inventory(inventory_path)
    require(
        expected_generation is None or inventory["generation"] == expected_generation,
        "unexpected inventory generation",
    )
    # Rendering a real inventory is read-only but its output must stay private.
    output = Path(output)
    if not inventory["example_only"]:
        require(
            ".private" in output.parts
            and (bundle is None or ".private" in Path(bundle).parts),
            "real inventory output must be under .private",
        )
    plan, details = build(inventory, specification)
    if bundle:
        bundle = Path(bundle)
        require(
            not bundle.exists() or not any(bundle.iterdir()),
            "bundle directory must be empty; preserve old render",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encode(plan))
    if bundle:
        bundle = Path(bundle)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "runtime-details.json").write_text(encode(details))
        (bundle / "inventory.yml").write_text(
            yaml.safe_dump(adapter(inventory), sort_keys=False)
        )
        for node in inventory["nodes"]:
            folder = bundle / "hosts" / node["name"]
            folder.mkdir(parents=True)
            files = service_configs(node, inventory)
            if node["role"] != "gateway":
                files["haproxy.cfg"] = haproxy(inventory)
            files["compose.yml"] = yaml.safe_dump(
                compose(node, inventory), sort_keys=False
            )
            files["details.json"] = encode(details["nodes"][node["name"]])
            files.update(systemd_units(node, inventory))
            daemon = {
                "log-driver": "local",
                "log-opts": {"max-size": "10m", "max-file": "3"},
                "live-restore": True,
            }
            if node["role"] != "gateway":
                daemon["proxies"] = {
                    "http-proxy": "http://127.0.0.1:3129",
                    "https-proxy": "http://127.0.0.1:3129",
                    "no-proxy": no_proxy(inventory),
                }
            else:
                files["tailscale-spec.json"] = encode(
                    {
                        "node": node["name"],
                        "state_path": "/srv/mlo/"
                        + inventory["generation"]
                        + "/tailscale/"
                        + node["name"],
                        "identity": "unresolved; unique per Gateway",
                        "copy_state_to_peer": False,
                        "tailscale_up_executed": False,
                        "model_path": "https://<accepted-home-node>.ts.net; Serve to localhost model API",
                        "public_model_port": False,
                    }
                )
            files["docker-daemon.required.json"] = encode(daemon)
            for name, contents in files.items():
                (folder / name).write_text(contents)
        for path in bundle.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
    output.chmod(0o600)
    return plan


def render(inventory_path, output, bundle=None, expected_generation=None):
    """Validate and stage every host before publishing; serialize competing renders.

    The bundle is one atomic directory rename. A separate plan path is published last;
    ordinary write/rename failures restore the previous bundle. Readers should use the
    canonical runtime-plan.json inside the bundle for an atomic plan+host snapshot.
    """
    output = Path(output).absolute()
    bundle = Path(bundle).absolute() if bundle else None
    require(not output.is_symlink(), "output symlink refused")
    require(bundle is None or not bundle.is_symlink(), "bundle symlink refused")
    require(bundle is None or (output != bundle and bundle not in output.parents),
            "external plan must not overlap bundle")
    parents = {output.parent, *(([bundle.parent]) if bundle else [])}
    for parent in parents:
        parent.mkdir(parents=True, exist_ok=True)
    locks, temporary, published, previous = [], [], False, None
    try:
        for target in sorted({output, *(([bundle]) if bundle else [])}, key=str):
            lock = os.open(str(target) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            locks.append(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ContractError("concurrent render/deploy refused") from exc
        stage = Path(tempfile.mkdtemp(prefix=".runtime-stage-", dir=output.parent))
        temporary.append(stage)
        staged_plan = stage / "plan.json"
        staged_bundle = None
        if bundle:
            staged_bundle = Path(tempfile.mkdtemp(prefix=".runtime-bundle-", dir=bundle.parent))
            temporary.append(staged_bundle)
        plan = _render_staged(inventory_path, staged_plan, staged_bundle, expected_generation)
        if bundle:
            (staged_bundle / "runtime-plan.json").write_text(encode(plan))
            (staged_bundle / "runtime-plan.json").chmod(0o600)
            if bundle.exists():
                require(bundle.is_dir(), "bundle target must be directory")
                previous = bundle.parent / (".runtime-previous-" + uuid.uuid4().hex)
                os.replace(bundle, previous)
            try:
                os.replace(staged_bundle, bundle)
                published = True
                os.replace(staged_plan, output)
            except BaseException:
                if published:
                    shutil.rmtree(bundle)
                    published = False
                if previous:
                    os.replace(previous, bundle)
                    previous = None
                raise
        else:
            os.replace(staged_plan, output)
        if previous:
            shutil.rmtree(previous)
            previous = None
        return plan
    finally:
        for path in temporary:
            if path.exists():
                shutil.rmtree(path)
        for lock in reversed(locks):
            os.close(lock)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--bundle", type=Path)
    p.add_argument("--expected-generation")
    args = p.parse_args()
    try:
        render(args.inventory, args.output, args.bundle, args.expected_generation)
    except (ContractError, ValueError, OSError, KeyError) as exc:
        print(
            f"FAIL: {type(exc).__name__}: runtime input/render rejected",
            file=sys.stderr,
        )
        return 2
    print("PASS: 16-node offline plan; ready_for_aws=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
