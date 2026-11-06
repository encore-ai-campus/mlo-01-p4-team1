"""Real Spark HA, authenticated TLS observations and PG lease-loss fencing.

Runs on one disposable Docker network, not AWS 3AZ or remote SSM.
Security namespaces: https://spark.apache.org/docs/4.1.3/security.html
"""

import json
import os
from pathlib import Path
import socket
import time
import unittest

from mlo_runtime.execution import DockerSparkExecution
from mlo_runtime.lab import Lab, certificates
from mlo_runtime.lease import PostgresLease
from mlo_runtime.operations import ActiveGroup, UnsafeTransition
from test_zookeeper import quorum_services


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SparkClusterIntegration(unittest.TestCase):
    def test_real_ha_drivers_executors_stop_on_pg_lease_loss(self):
        lab = Lab("spark-fence")
        self.addCleanup(lab.close)
        services = quorum_services(lab)
        certs = lab.folder / "spark-pki"
        certs.mkdir()
        certificates(
            certs,
            [f"m{i}" for i in (1, 2, 3)]
            + [f"w{i}" for i in (1, 2, 3)]
            + ["driver1", "driver2"],
        )
        (lab.folder / "hold.py").write_text(
            "from pyspark.sql import SparkSession\nimport time\n"
            "spark=SparkSession.builder.getOrCreate()\n"
            "spark.sparkContext.parallelize([1],1).map(lambda x: time.sleep(600)).collect()\n"
        )
        common = {
            "spark.authenticate": "true",
            "spark.authenticate.secret": "local-fixture",
            "spark.ssl.rpc.enabled": "true",
            "spark.ssl.enabled": "true",
            "spark.ssl.protocol": "TLSv1.2",
            "spark.ssl.keyStore": "/pki/server.p12",
            "spark.ssl.keyStorePassword": "fixture",
            "spark.ssl.keyPassword": "fixture",
            "spark.ssl.keyStoreType": "PKCS12",
            "spark.ssl.trustStore": "/seed/trust.p12",
            "spark.ssl.trustStorePassword": "fixture",
            "spark.ssl.trustStoreType": "PKCS12",
            "spark.ssl.standalone.needClientAuth": "true",
            "spark.ssl.standalone.port": "8443",
            "spark.port.maxRetries": "0",
            "spark.ui.enabled": "false",
            "spark.executor.cores": "1",
            "spark.cores.max": "1",
            "spark.executor.memory": "512m",
        }
        (lab.folder / "spark-defaults.conf").write_text(
            "".join(key + " " + value + "\n" for key, value in common.items())
        )
        # Java properties are the ZooKeeper client's TLS inputs; no plaintext
        # client listener is created by quorum_services.
        zk_tls = " ".join(
            [
                "-Dzookeeper.client.secure=true",
                "-Dzookeeper.clientCnxnSocket=org.apache.zookeeper.ClientCnxnSocketNetty",
                "-Dzookeeper.ssl.keyStore.location=/seed/z1.p12",
                "-Dzookeeper.ssl.keyStore.password=fixture",
                "-Dzookeeper.ssl.trustStore.location=/seed/trust.p12",
                "-Dzookeeper.ssl.trustStore.password=fixture",
            ]
        )
        master_options = " ".join(
            [
                "-Dspark.deploy.recoveryMode=ZOOKEEPER",
                "-Dspark.deploy.zookeeper.url=z1:2182,z2:2182,z3:2182",
                "-Dspark.deploy.zookeeper.dir=/local-fence",
                "-Dspark.deploy.recoveryTimeout=10s",
                *["-D" + k + "=" + v for k, v in common.items()],
                zk_tls,
            ]
        )
        platform = os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64")
        masters, workers = [], []
        for kind in ("m", "w"):
            for i in (1, 2, 3):
                name, port = f"{kind}{i}", free_port()
                (masters if kind == "m" else workers).append(
                    f"https://127.0.0.1:{port}/json/"
                )
                command = [
                    "/opt/spark/bin/spark-class",
                    "org.apache.spark.deploy."
                    + ("master.Master" if kind == "m" else "worker.Worker"),
                    "--host",
                    name,
                    "--port",
                    "7077" if kind == "m" else "7078",
                    "--webui-port",
                    "8080",
                ]
                if kind == "w":
                    command += [
                        "--cores",
                        "1",
                        "--memory",
                        "768m",
                        "spark://m1:7077,m2:7077,m3:7077",
                    ]
                services[name] = {
                    "image": lab.image("spark"),
                    "platform": platform,
                    "user": "0:0",
                    "hostname": name,
                    "mem_limit": "384m" if kind == "m" else "1g",
                    "environment": {
                        "SPARK_DAEMON_MEMORY": "192m",
                        "SPARK_MASTER_OPTS": master_options,
                        "SPARK_WORKER_OPTS": " ".join(
                            "-D" + k + "=" + v for k, v in common.items()
                        ),
                    },
                    "ports": [f"127.0.0.1:{port}:8443"],
                    "volumes": [
                        f"{lab.folder}:/seed:ro",
                        f"{certs}:/pki:ro",
                        f"{lab.folder}/spark-defaults.conf:/opt/spark/conf/spark-defaults.conf:ro",
                    ],
                    "entrypoint": command,
                }
        token = {
            "owner": "control-a",
            "generation": "local-fence",
            "epoch": 1,
            "expires_at": time.time() + 900,
        }
        for i in (1, 2):
            services[f"driver{i}"] = {
                "image": lab.image("spark"),
                "platform": platform,
                "user": "0:0",
                "hostname": f"driver{i}",
                "mem_limit": "1g",
                "labels": {
                    "mlo." + k: str(token[k]) for k in ("owner", "generation", "epoch")
                },
                "volumes": [
                    f"{lab.folder}:/seed:ro",
                    f"{certs}:/pki:ro",
                    f"{lab.folder}/spark-defaults.conf:/opt/spark/conf/spark-defaults.conf:ro",
                ],
                "entrypoint": [
                    "/opt/spark/bin/spark-submit",
                    "--master",
                    "spark://m1:7077,m2:7077,m3:7077",
                    "--driver-memory",
                    "512m",
                    "--conf",
                    f"spark.driver.host=driver{i}",
                    "--name",
                    f"B-fence-driver-{i}",
                    "/seed/hold.py",
                ],
            }
        pg_port = free_port()
        services["pg"] = {
            "image": lab.image("postgresql"),
            "platform": platform,
            "mem_limit": "256m",
            "environment": {"POSTGRES_PASSWORD": "fixture"},
            "ports": [f"127.0.0.1:{pg_port}:5432"],
            "volumes": ["pg:/var/lib/postgresql/data"],
        }
        services["pki-init"] = {
            "image": lab.image("zookeeper"),
            "platform": platform,
            "user": "0:0",
            "mem_limit": "256m",
            "volumes": [f"{lab.folder}:/seed"],
            "entrypoint": [
                "/bin/bash",
                "-ec",
                "keytool -importcert -noprompt -alias zk -file /seed/ca.crt "
                "-keystore /seed/trust.p12 -storetype PKCS12 -storepass fixture; "
                "keytool -importcert -noprompt -alias spark -file /seed/spark-pki/ca.crt "
                "-keystore /seed/trust.p12 -storetype PKCS12 -storepass fixture",
            ],
        }
        for service in services.values():
            service.setdefault("labels", {}).update(
                {"mlo.role": "B", "mlo.run": lab.project}
            )
        lab.compose_path.write_text(
            json.dumps({"services": services, "volumes": {"pg": {}}})
        )
        lab.compose("run", "--rm", "pki-init", timeout=60)
        lab.compose(
            "up",
            "-d",
            *[n for n in services if not n.startswith("driver") and n != "pki-init"],
            timeout=600,
        )
        execution = DockerSparkExecution(
            (),
            tuple(masters),
            tuple(workers),
            str(certs / "ca.crt"),
            str(certs / "server.crt"),
            str(certs / "server.key"),
        )

        def wait_for(predicate, message, seconds=150):
            last = None
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                try:
                    value = predicate()
                    if value:
                        return value
                except Exception as exc:
                    last = exc
                time.sleep(1)
            self.fail(f"{message}: {last}; see {lab.folder}")

        def cluster_ready():
            ms = [execution._json(url) for url in masters]
            ws = [execution._json(url) for url in workers]
            return sum(m["status"] == "ALIVE" for m in ms) == 1 and all(
                w["masterurl"]
                in ("spark://m1:7077", "spark://m2:7077", "spark://m3:7077")
                for w in ws
            )

        wait_for(cluster_ready, "TLS HA cluster not ready")
        lab.compose("up", "-d", "driver1", "driver2", timeout=120)
        ids = [
            lab.compose("ps", "-q", f"driver{i}").stdout.decode().strip()
            for i in (1, 2)
        ]
        execution.containers = tuple(ids)
        # Worker JSON serializes allocated executors without a state field.
        # Require two registered applications and their actual allocated cores.
        wait_for(
            lambda: sum(len(execution._json(url)["executors"]) for url in workers) >= 2
            and sum(execution._json(url)["coresused"] for url in workers) >= 2,
            "two real executors did not start",
        )
        initial = [execution._json(url) for url in masters]
        leader = next(
            i for i, item in enumerate(initial, 1) if item["status"] == "ALIVE"
        )
        lab.compose("stop", f"m{leader}")
        wait_for(
            lambda: any(
                execution._json(url)["status"] == "ALIVE"
                for i, url in enumerate(masters, 1)
                if i != leader
            ),
            "standby master did not recover",
        )
        lab.compose("start", f"m{leader}")
        wait_for(cluster_ready, "all master observers did not recover")
        lease = PostgresLease(
            f"host=127.0.0.1 port={pg_port} user=postgres password=fixture dbname=postgres sslmode=disable",
            "spark-fence",
            lab=True,
        )
        lease.initialize()
        token["expires_at"] = lease.now() + 15
        self.assertTrue(lease.cas(None, token))
        group = ActiveGroup(
            lab.folder / "group.json",
            token["generation"],
            token["owner"],
            lease,
            execution,
        )
        # Break the actual lease DB connection; stop is initiated by tick's
        # exception path, followed by six live authenticated TLS observations.
        lab.compose("stop", "pg")
        with self.assertRaises(UnsafeTransition):
            group.tick(token)
        wait_for(
            lambda: execution.observe(token).cluster_idle
            and execution.observe(token).executors_absent,
            "executors survived lease loss",
            seconds=90,
        )
        group._fence(token)
        saved = json.loads(group.state.read_text())
        self.assertEqual(saved["phase"], "fenced")
        self.assertEqual(saved["old_owner"], token)
        execution.observe(token).validate(
            token["owner"], token["generation"], 1, time.time()
        )
