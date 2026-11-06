import json
from pathlib import Path
import unittest

from mlo_runtime.contracts import RUNTIME
from mlo_runtime.lab import Lab, certificates
from mlo_runtime.operations import backup


class DockerIntegration(unittest.TestCase):
    def lab(self, service):
        lab = Lab(service)
        self.addCleanup(lab.close)
        return lab

    def test_ansible_actual_linux_reapply_permissions_and_persistence(self):
        lab = self.lab("host")
        lab.configure(
            {
                "image": "mlo-p4-b-host-lab:20261029",
                "volumes": ["data:/srv/mlo", f"{RUNTIME}:/workspace:ro"],
                "command": ["sleep", "infinity"],
            }
        )
        lab.up()
        command = [
            "ansible-playbook",
            "-i",
            "localhost,",
            "-c",
            "local",
            "/workspace/ansible/lab.yml",
        ]
        first = lab.exec(*command)
        self.assertIn(b"failed=0", first.stdout)
        second = lab.exec(*command)
        self.assertRegex(
            second.stdout.decode(), r"changed=0\s+unreachable=0\s+failed=0"
        )
        lab.exec(
            "python3",
            "-c",
            "from pathlib import Path; p=Path('/srv/mlo/local-test/spool'); assert p.stat().st_uid==2000; assert p.stat().st_mode & 0o777==0o750; (p/'preserve').write_text('keep')",
        )
        lab.recreate()
        lab.exec(
            "python3",
            "-c",
            "from pathlib import Path; assert Path('/srv/mlo/local-test/spool/preserve').read_text()=='keep'",
        )

    def test_postgresql_tls_auth_backup_restore_recreate(self):
        lab = self.lab("postgresql")
        certificates(lab.folder)
        lab.configure(
            {
                "image": lab.image("postgresql"),
                "environment": {
                    "POSTGRES_USER": "mlo",
                    "POSTGRES_PASSWORD": "fixture",
                    "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256",
                    "PGPASSWORD": "fixture",
                    "PGSSLMODE": "verify-full",
                    "PGSSLROOTCERT": "/seed/ca.crt",
                },
                "volumes": ["data:/var/lib/postgresql/data", f"{lab.folder}:/seed:ro"],
                "entrypoint": [
                    "bash",
                    "-ec",
                    "cp /seed/server.crt /seed/server.key /tmp/; chown postgres:postgres /tmp/server.*; chmod 600 /tmp/server.key; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key",
                ],
            }
        )
        lab.up()
        sql = [
            "psql",
            "-h",
            "localhost",
            "-U",
            "mlo",
            "-d",
            "mlo",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
        ]
        lab.ready([*sql, "select 1"])
        lab.exec(
            *sql,
            "create table acceptance(id integer primary key, body text); insert into acceptance values (1,'preserved')",
        )
        bad = lab.exec("env", "PGPASSWORD=wrong", *sql, "select 1", check=False)
        self.assertNotEqual(bad.returncode, 0)
        command = [
            "docker",
            "compose",
            "--project-name",
            lab.project,
            "--file",
            str(lab.compose_path),
            "exec",
            "-T",
            lab.service,
            "pg_dump",
            "-h",
            "localhost",
            "-U",
            "mlo",
            "-Fc",
            "mlo",
        ]
        archive = backup(
            command,
            lab.folder / "verified.dump",
            lambda p: p.read_bytes().startswith(b"PGDMP"),
        )
        lab.exec(*sql, "drop table acceptance")
        lab.compose("cp", str(archive), "postgresql:/tmp/verified.dump")
        lab.exec(
            "pg_restore",
            "-h",
            "localhost",
            "-U",
            "mlo",
            "-d",
            "mlo",
            "--exit-on-error",
            "/tmp/verified.dump",
        )
        lab.recreate()
        lab.ready([*sql, "select body from acceptance where id=1"])
        self.assertEqual(
            lab.exec(*sql, "select body from acceptance where id=1").stdout.strip(),
            b"preserved",
        )

    def test_mongodb_tls_auth_backup_restore_recreate(self):
        lab = self.lab("mongodb")
        certificates(lab.folder)
        # Disposable per-run key, never an operational key or an existing database.
        (lab.folder / "mongodb-keyfile").write_text("bG9jYWwtb25seS1yZXBsaWNhLXNldC1maXh0dXJlLTAwMDAwMDAw")
        lab.configure(
            {
                "image": lab.image("mongodb"),
                "environment": {
                    "MONGO_INITDB_ROOT_USERNAME": "mlo",
                    "MONGO_INITDB_ROOT_PASSWORD": "fixture",
                },
                "volumes": ["data:/data/db", f"{lab.folder}:/seed:ro"],
                "entrypoint": [
                    "bash",
                    "-ec",
                    "cp /seed/server.pem /tmp/server.pem; cp /seed/mongodb-keyfile /tmp/mongodb-keyfile; chown mongodb:mongodb /tmp/server.pem /tmp/mongodb-keyfile; chmod 600 /tmp/server.pem /tmp/mongodb-keyfile; exec docker-entrypoint.sh mongod --replSet ads-rs --keyFile /tmp/mongodb-keyfile --bind_ip_all --tlsMode requireTLS --tlsCertificateKeyFile /tmp/server.pem --tlsCAFile /seed/ca.crt --tlsAllowConnectionsWithoutCertificates",
                ],
            }
        )
        lab.up()
        connection = [
            "--host",
            "localhost",
            "--tls",
            "--tlsCAFile",
            "/seed/ca.crt",
            "--username",
            "mlo",
            "--password",
            "fixture",
            "--authenticationDatabase",
            "admin",
        ]
        shell = ["mongosh", *connection, "--quiet", "--eval"]
        lab.ready([*shell, "db.runCommand({ping:1})"])
        lab.exec(*shell, "rs.initiate({_id:'ads-rs',members:[{_id:0,host:'localhost:27017'}]})")
        lab.ready([*shell, "quit(db.hello().isWritablePrimary ? 0 : 1)"])
        lab.exec(
            *shell,
            "db.getSiblingDB('acceptance').events.insertOne({_id:1,body:'preserved'})",
        )
        lab.exec(*shell, "const s=db.getMongo().startSession();s.startTransaction();s.getDatabase('acceptance').events.insertOne({_id:2,body:'transaction'});s.commitTransaction();s.endSession();if(db.getSiblingDB('acceptance').events.countDocuments({_id:2})!==1)quit(2)")
        bad = lab.exec(
            "mongosh",
            "--host",
            "localhost",
            "--tls",
            "--tlsCAFile",
            "/seed/ca.crt",
            "--quiet",
            "--eval",
            "db.getSiblingDB('acceptance').events.findOne()",
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)
        tools_connection = [
            "--ssl" if a == "--tls" else "--sslCAFile" if a == "--tlsCAFile" else a
            for a in connection
        ]
        archive = lab.exec(
            "mongodump", *tools_connection, "--db", "acceptance", "--archive", "--gzip"
        ).stdout
        self.assertTrue(len(archive) > 100)
        (lab.folder / "mongo.archive.gz").write_bytes(archive)
        lab.exec(*shell, "db.getSiblingDB('acceptance').dropDatabase()")
        lab.exec(
            "mongorestore", *tools_connection, "--archive", "--gzip", input=archive
        )
        lab.recreate()
        lab.ready([*shell, "db.runCommand({ping:1})"])
        self.assertIn(
            b"preserved",
            lab.exec(
                *shell, "db.getSiblingDB('acceptance').events.findOne({_id:1}).body"
            ).stdout,
        )

    def test_kafka_tls_produce_consume_and_persistent_restart(self):
        lab = self.lab("kafka")
        certificates(lab.folder)
        config = (
            "process.roles=broker,controller\nnode.id=1\ncontroller.quorum.voters=1@localhost:9094\n"
            "listeners=CLIENT://:9093,CONTROLLER://:9094\nadvertised.listeners=CLIENT://localhost:9093\n"
            "listener.security.protocol.map=CLIENT:SSL,CONTROLLER:SSL\ncontroller.listener.names=CONTROLLER\n"
            "inter.broker.listener.name=CLIENT\nssl.client.auth=required\nssl.keystore.type=PKCS12\n"
            "ssl.keystore.location=/seed/server.p12\nssl.keystore.password=fixture\nssl.key.password=fixture\n"
            "ssl.truststore.type=PKCS12\nssl.truststore.location=/tmp/truststore.p12\nssl.truststore.password=fixture\n"
            "log.dirs=/data/kafka\nnum.partitions=1\ndefault.replication.factor=1\nmin.insync.replicas=1\n"
            "offsets.topic.replication.factor=1\ntransaction.state.log.replication.factor=1\ntransaction.state.log.min.isr=1\n"
        )
        (lab.folder / "server.properties").write_text(config)
        (lab.folder / "client.properties").write_text(
            "security.protocol=SSL\nssl.keystore.type=PKCS12\nssl.keystore.location=/seed/server.p12\nssl.keystore.password=fixture\nssl.key.password=fixture\nssl.truststore.type=PKCS12\nssl.truststore.location=/tmp/truststore.p12\nssl.truststore.password=fixture\nrequest.timeout.ms=5000\ndefault.api.timeout.ms=10000\n"
        )
        lab.configure(
            {
                "image": lab.image("kafka"),
                "user": "0:0",
                "environment": {"KAFKA_HEAP_OPTS": "-Xms256m -Xmx512m"},
                "volumes": ["data:/data/kafka", f"{lab.folder}:/seed:ro"],
                "entrypoint": [
                    "/bin/bash",
                    "-ec",
                    'keytool -importcert -noprompt -alias ca -file /seed/ca.crt -keystore /tmp/truststore.p12 -storetype PKCS12 -storepass fixture; if [ ! -f /data/kafka/meta.properties ]; then test -z "$(ls -A /data/kafka)"; /opt/kafka/bin/kafka-storage.sh format -t 3Db5Lwr0R6KpX5a1Jq2Z8w -c /seed/server.properties; fi; exec /opt/kafka/bin/kafka-server-start.sh /seed/server.properties',
                ],
            }
        )
        lab.up()
        base = ["--bootstrap-server", "localhost:9093"]
        lab.ready(
            [
                "/opt/kafka/bin/kafka-topics.sh",
                *base,
                "--command-config",
                "/seed/client.properties",
                "--list",
            ],
            attempts=45,
        )
        lab.exec(
            "/opt/kafka/bin/kafka-topics.sh",
            *base,
            "--command-config",
            "/seed/client.properties",
            "--create",
            "--topic",
            "acceptance",
            "--partitions",
            "1",
            "--replication-factor",
            "1",
        )
        lab.exec(
            "/opt/kafka/bin/kafka-console-producer.sh",
            *base,
            "--producer.config",
            "/seed/client.properties",
            "--topic",
            "acceptance",
            "--producer-property",
            "acks=all",
            input=b"preserved\n",
        )
        consume = [
            "/opt/kafka/bin/kafka-console-consumer.sh",
            *base,
            "--consumer.config",
            "/seed/client.properties",
            "--topic",
            "acceptance",
            # Direct assignment isolates log persistence from fresh consumer
            # group coordination during broker recovery.
            "--partition",
            "0",
            "--offset",
            "earliest",
            "--max-messages",
            "1",
            "--timeout-ms",
            "15000",
        ]
        self.assertIn(b"preserved", lab.exec(*consume).stdout)
        lab.recreate()
        lab.ready(
            [
                "/opt/kafka/bin/kafka-topics.sh",
                *base,
                "--command-config",
                "/seed/client.properties",
                "--list",
            ],
            attempts=45,
        )
        self.assertIn(b"preserved", lab.exec(*consume).stdout)

    def test_elasticsearch_tls_auth_persistent_document(self):
        lab = self.lab("elasticsearch")
        certificates(lab.folder)
        lab.configure(
            {
                "image": lab.image("elasticsearch"),
                "mem_limit": "2g",
                "environment": {
                    "discovery.type": "single-node",
                    "ELASTIC_PASSWORD": "local-fixture-password",
                    "ES_JAVA_OPTS": "-Xms512m -Xmx512m",
                    "xpack.security.enabled": "true",
                    "xpack.security.http.ssl.enabled": "true",
                    "xpack.security.http.ssl.key": "certs/server.key",
                    "xpack.security.http.ssl.certificate": "certs/server.crt",
                },
                "volumes": [
                    "data:/usr/share/elasticsearch/data",
                    f"{lab.folder}:/seed:ro",
                    f"{lab.folder}:/usr/share/elasticsearch/config/certs:ro",
                ],
            }
        )
        lab.up()
        curl = [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--cacert",
            "/seed/ca.crt",
            "-u",
            "elastic:local-fixture-password",
        ]
        lab.ready(
            [*curl, "https://localhost:9200/_cluster/health?wait_for_status=yellow"]
        )
        no_auth = lab.exec(
            "curl",
            "--silent",
            "--cacert",
            "/seed/ca.crt",
            "-w",
            "%{http_code}",
            "https://localhost:9200/",
        )
        self.assertTrue(no_auth.stdout.endswith(b"401"))
        lab.exec(
            *curl,
            "-X",
            "PUT",
            "-H",
            "Content-Type: application/json",
            "--data",
            '{"body":"preserved"}',
            "https://localhost:9200/acceptance/_doc/1?refresh=true",
        )
        lab.recreate()
        lab.ready(
            [*curl, "https://localhost:9200/_cluster/health?wait_for_status=yellow"]
        )
        self.assertEqual(
            json.loads(
                lab.exec(*curl, "https://localhost:9200/acceptance/_doc/1").stdout
            )["_source"]["body"],
            "preserved",
        )


if __name__ == "__main__":
    unittest.main()
