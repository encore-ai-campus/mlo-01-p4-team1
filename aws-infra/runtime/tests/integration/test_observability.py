import datetime
import json
import time
import unittest
import uuid

import yaml

from mlo_runtime.observability import (
    BoundedSpool,
    dashboard,
    filebeat_config,
    kibana_config,
    logstash_pipeline,
    logstash_settings,
    security_artifacts,
    pressure,
)
from mlo_runtime.projection import ProjectionRunner
from track2_lab import elastic_lab, service, start_es, wait_for
from observation_install_fixture import install_observation_files


def fixture(state="COMMITTED", dataset="mlo.pipeline"):
    return {
        "@timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": {
            "id": str(uuid.uuid4()),
            "dataset": dataset,
            "action": "fixture.transport",
            "outcome": "unknown",
        },
        "service": {"name": "fixture"},
        "host": {"name": "control-a"},
        "cloud": {
            "provider": "aws",
            "region": "ap-northeast-2",
            "account": {"name": "encore1"},
        },
        "pipeline": {
            "name": "fixture",
            "run_id": str(uuid.uuid4()),
            "stage": "transport",
            "state": state,
            "attempt": 1,
        },
        "message": "NEVER_STORE_PASSWORD_OR_EMAIL@example.test",
        "request": {"cookie": "NEVER_STORE_COOKIE"},
    }


class ObservationAcceptance(unittest.TestCase):
    def test_real_mtls_ingest_kibana_duplicate_collector_es_outages_and_projection(
        self,
    ):
        lab, config = elastic_lab("observations")
        self.addCleanup(lab.close)
        spool = BoundedSpool(lab.folder / "spool")
        # Lab values are synthetic; production uses protected component keystores.
        fb_config = filebeat_config("logstash")
        # Bridge-only lab metrics access; production stays bound to host loopback.
        fb_config["http.host"] = "0.0.0.0"
        (lab.folder / "filebeat.yml").write_text(yaml.safe_dump(fb_config))
        (lab.folder / "logstash.yml").write_text(
            yaml.safe_dump(logstash_settings("logstash"))
        )
        (lab.folder / "logstash.conf").write_text(
            logstash_pipeline("0.0.0.0", ["https://elasticsearch:9200"])
        )
        kc = kibana_config("0.0.0.0", ["https://elasticsearch:9200"])
        kc.update(
            {
                "xpack.security.encryptionKey": "a" * 32,
                "xpack.encryptedSavedObjects.encryptionKey": "b" * 32,
                "xpack.reporting.encryptionKey": "c" * 32,
            }
        )
        (lab.folder / "kibana.yml").write_text(yaml.safe_dump(kc))
        (lab.folder / "observations.ndjson").write_text(dashboard())
        config["services"].update(
            {
                "logstash": service(
                    lab,
                    "logstash",
                    user="1000:0",
                    command=["logstash", "--path.settings", "/run/mlo/secrets", "-f", "/run/mlo/secrets/logstash.conf"],
                    environment={
                        "LS_JAVA_OPTS": "-Xms512m -Xmx512m -XX:MaxDirectMemorySize=256m",
                    },
                    volumes=[
                        "installed:/run/mlo/secrets:ro",
                        "pq:/data/logstash",
                    ],
                ),
                "kibana": service(
                    lab,
                    "kibana",
                    user="1000:0",
                    environment={
                        "NODE_OPTIONS": "--max-old-space-size=768",
                        "ELASTIC_APM_ACTIVE": "false",
                    },
                    volumes=[
                        "installed:/run/mlo/secrets:ro",
                        "kb:/usr/share/kibana/data",
                        "installed:/usr/share/kibana/config:ro",
                    ],
                ),
                "filebeat": service(
                    lab,
                    "filebeat",
                    mem_limit="256m",
                    cpus=0.25,
                    user="0:0",
                    command=[
                        "filebeat",
                        "-e",
                        "--strict.perms=false",
                        "-c",
                        "/etc/filebeat.yml",
                    ],
                    volumes=[
                        f"{lab.folder}:/run/mlo/secrets:ro",
                        f"{spool.folder}:/spool:ro",
                        "fb:/data/filebeat",
                        f"{lab.folder / 'filebeat.yml'}:/etc/filebeat.yml:ro",
                    ],
                ),
            }
        )
        config["volumes"].update({"pq": {}, "kb": {}, "fb": {}})
        install_observation_files(lab, config)
        # Keystores are provisioned as the same accepted non-root UID. Secrets
        # are synthetic and stdin is not included in Lab's command transcript.
        config["services"]["keystore-logstash"] = service(
            lab, "logstash", user="1000:0", mem_limit="512m",
            environment={"LS_JAVA_OPTS":"-Xms128m -Xmx128m"},
            volumes=["installed:/run/mlo/secrets", "pq:/data/logstash"],
            entrypoint=["/usr/share/logstash/bin/logstash-keystore", "--path.settings", "/run/mlo/secrets"])
        config["services"]["keystore-kibana"] = service(
            lab, "kibana", user="1000:0", mem_limit="512m",
            volumes=["installed:/usr/share/kibana/config"],
            entrypoint=["/usr/share/kibana/bin/kibana-keystore"])
        lab.compose_path.write_text(json.dumps(config))
        lab.compose("run", "--rm", "-T", "keystore-logstash", "create", input=b"y\n", timeout=120)
        for key, value in (("MLO_LOG_WRITER", "fixture-log-writer"), ("MLO_LOG_PASSWORD", "fixture-log")):
            lab.compose("run", "--rm", "-T", "keystore-logstash", "add", key, "--stdin", input=(value+"\n").encode(), timeout=120)
        lab.compose("run", "--rm", "-T", "keystore-kibana", "create", timeout=120)
        lab.compose("run", "--rm", "-T", "keystore-kibana", "add", "elasticsearch.password", "--stdin", input=b"fixture-kibana\n", timeout=120)
        es = start_es(lab)
        sec = security_artifacts()
        es.request("PUT", "/_index_template/mlo-observations-v02", sec["template"])
        for role in ("writer", "reader"):
            es.request("PUT", "/_security/role/mlo_logs_" + role, sec[role])
        es.request(
            "PUT",
            "/_security/user/fixture-log-writer",
            {"password": "fixture-log", "roles": ["mlo_logs_writer"]},
        )
        es.request(
            "POST",
            "/_security/user/kibana_system/_password",
            {"password": "fixture-kibana"},
        )
        lab.compose("up", "-d", "logstash", "kibana", "filebeat", timeout=180)

        def exec_service(name, *args, **kwargs):
            if args and args[0] == "curl":
                args = (args[0], "--max-time", "5", *args[1:])
            return lab.compose("exec", "-T", name, *args, **kwargs)

        for name in ("logstash", "kibana"):
            self.assertEqual(exec_service(name, "id", "-u").stdout.strip(), b"1000")
            key = "/run/mlo/secrets/" + name + ".keystore"
            if name == "kibana":
                key = "/usr/share/kibana/config/kibana.keystore"
            self.assertEqual(exec_service(name, "stat", "-c", "%u", key).stdout.strip(), b"1000")

        wait_for(
            lambda: exec_service(
                "logstash",
                "curl",
                "--fail",
                "--silent",
                "http://127.0.0.1:9600/_node/stats/pipelines/main",
                check=False,
            ).returncode
            == 0
        )
        # Configuration, actual output TLS and unauthenticated-client rejection.
        wait_for(
            lambda: exec_service(
                "filebeat",
                "filebeat",
                "test",
                "output",
                "--strict.perms=false",
                "-c",
                "/etc/filebeat.yml",
                check=False,
            ).returncode
            == 0
        )
        test = exec_service(
            "filebeat",
            "filebeat",
            "test",
            "output",
            "--strict.perms=false",
            "-c",
            "/etc/filebeat.yml",
        )
        self.assertIn(b"TLS", test.stdout + test.stderr)
        bad = exec_service(
            "elasticsearch",
            "curl",
            "--max-time",
            "5",
            "--fail",
            "--cacert",
            "/usr/share/elasticsearch/config/lab/ca.crt",
            "https://logstash:5044",
            check=False,
        )
        self.assertNotEqual(bad.returncode, 0)

        def docs():
            es.request("POST", "/logs-mlo.*-*/_refresh")
            result = es.request("GET", "/logs-mlo.*-*/_search?size=100", missing=True)
            return {
                d["_id"]: d["_source"]
                for d in (result or {}).get("hits", {}).get("hits", [])
            }

        expected = set()
        first = fixture("SENT")
        expected.add(first["event"]["id"])
        spool.append(first)
        spool.append(first)
        wait_for(lambda: expected <= docs().keys())
        self.assertEqual(len(docs()), 1)
        self.assertNotIn("NEVER_STORE", json.dumps(docs()))
        self.assertEqual(docs()[first["event"]["id"]]["pipeline"]["state"], "SENT")
        def agent_stats():
            result = exec_service("elasticsearch", "curl", "--fail", "--silent", "http://filebeat:5066/stats")
            return json.loads(result.stdout)

        # Diskqueue filled can include already ACKed data pending segment cleanup.
        # Record counters first so retained old data cannot satisfy this test.
        def initially_acked():
            sample = agent_stats()
            return sample if sample["libbeat"]["output"]["events"]["acked"] >= 2 else None

        before_collector = wait_for(initially_acked)
        # Stop collector and retain input/diskqueue/registry across agent recreation.
        lab.compose("stop", "logstash")
        during_collector = fixture("ACKNOWLEDGED")
        expected.add(during_collector["event"]["id"])
        spool.append(during_collector)
        def queued_agent():
            sample = agent_stats()
            read_new_event = sample["filebeat"]["events"]["added"] > before_collector["filebeat"]["events"]["added"]
            not_acked = sample["libbeat"]["output"]["events"]["acked"] == before_collector["libbeat"]["output"]["events"]["acked"]
            queued = sample["libbeat"]["pipeline"]["queue"]["filled"]["events"] >= 1
            return sample if read_new_event and not_acked and queued else None
        agent_sample = wait_for(queued_agent)
        (lab.folder / "filebeat-during-collector-outage.json").write_text(json.dumps(agent_sample, indent=2))
        self.assertNotIn(during_collector["event"]["id"], docs())
        stats = exec_service(
            "filebeat",
            "filebeat",
            "test",
            "output",
            "--strict.perms=false",
            "-c",
            "/etc/filebeat.yml",
            check=False,
        )
        self.assertNotEqual(stats.returncode, 0)
        lab.compose("up", "-d", "--force-recreate", "filebeat", timeout=120)
        lab.compose("start", "logstash")
        wait_for(lambda: expected <= docs().keys())
        # ES outage: Logstash retains a persistent queue and retries after restart.
        lab.compose("stop", "elasticsearch")
        # PQ events_count counts unread work, not the batch already held by a
        # worker retrying Elasticsearch. Exceed that batch to prove real backlog,
        # then require every event after queue-preserving collector recreation.
        settings = logstash_settings("logstash")
        outage_count = settings["pipeline.workers"] * settings["pipeline.batch.size"] * 2 + 1
        for _ in range(outage_count):
            during_es = fixture("COMMITTED")
            expected.add(during_es["event"]["id"])
            spool.append(during_es)
        def queued_collector():
            result = exec_service("logstash", "curl", "--fail", "--silent", "http://127.0.0.1:9600/_node/stats/pipelines/main")
            sample = json.loads(result.stdout)
            return sample if sample["pipelines"]["main"]["queue"]["events_count"] >= 1 else None
        pq_sample = wait_for(queued_collector)
        (lab.folder / "pq-during-es-outage.json").write_text(json.dumps(pq_sample, indent=2))
        lab.compose("stop", "logstash", timeout=120)
        # Docker can allocate a different ephemeral host port after stop/start.
        # Re-discover the verified HTTPS endpoint rather than retrying a stale URL.
        es = start_es(lab)
        wait_for(
            lambda: es.request("GET", "/_cluster/health")["status"]
            in {"yellow", "green"}
        )
        lab.compose("up", "-d", "--force-recreate", "logstash", timeout=120)
        wait_for(lambda: expected <= docs().keys())
        # Generic accepted exporter cursor plumbing, not an ads business worker.
        observation = fixture("COMMITTED", "mlo.ads")
        ledger = lab.folder / "synthetic-ledger.json"
        ledger.write_text(
            json.dumps(
                {
                    "log_record": [{"stable_id": observation["event"]["id"]}],
                    "budget": 123,
                }
            )
        )
        before = ledger.read_bytes()

        def export(cursor, limit):
            json.loads(ledger.read_bytes())
            return {
                "events": [observation, observation] if cursor is None else [],
                "cursor": "read-only-1",
                "backlog": 1,
            }

        now = [1000]
        runner = ProjectionRunner(
            lab.folder / "cursor.json",
            spool,
            export,
            lambda: True,
            fixture("HEARTBEAT", "mlo.ads"),
            clock=lambda: now[0],
        )
        runner.step()
        now[0] += 121
        self.assertEqual(runner.step()["state"], "STALE")
        self.assertEqual(ledger.read_bytes(), before)
        expected.add(observation["event"]["id"])
        wait_for(
            lambda: expected <= docs().keys()
            and any(d["pipeline"]["state"] == "STALE" for d in docs().values())
        )
        self.assertNotIn("NEVER_STORE", json.dumps(docs()))
        for file in spool.folder.glob("*.ndjson"):
            self.assertNotIn("NEVER_STORE", file.read_text())

        def kibana(
            path,
            *,
            method="GET",
            body=None,
            form=None,
            auth=True,
            credential="elastic:fixture",
        ):
            args = [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--cacert",
                "/usr/share/elasticsearch/config/lab/ca.crt",
                "-H",
                "kbn-xsrf: fixture",
                "-X",
                method,
            ]
            if auth:
                args += ["-u", credential]
            if body is not None:
                args += [
                    "-H",
                    "Content-Type: application/json",
                    "--data",
                    json.dumps(body),
                ]
            if form is not None:
                args += ["-F", form]
            return exec_service(
                "elasticsearch", *args, "https://kibana:5601" + path, check=False
            )

        wait_for(lambda: kibana("/api/status").returncode == 0, seconds=240)
        self.assertNotEqual(
            kibana("/api/saved_objects/_find?type=dashboard", auth=False).returncode, 0
        )
        self.assertEqual(
            kibana(
                "/api/spaces/space",
                method="POST",
                body={"id": "observations", "name": "Observations"},
            ).returncode,
            0,
        )
        self.assertEqual(
            kibana(
                "/api/security/role/mlo_observation_operator",
                method="PUT",
                body=sec["kibana_operator"],
            ).returncode,
            0,
        )
        imported = kibana(
            "/s/observations/api/saved_objects/_import?overwrite=true",
            method="POST",
            form="file=@/usr/share/elasticsearch/config/lab/observations.ndjson",
        )
        (lab.folder / "kibana-import.json").write_bytes(imported.stdout)
        self.assertEqual(imported.returncode, 0)
        self.assertTrue(json.loads(imported.stdout)["success"])
        saved = kibana(
            "/s/observations/api/saved_objects/dashboard/mlo-observations-v02"
        )
        self.assertEqual(saved.returncode, 0)
        self.assertEqual(
            json.loads(saved.stdout)["attributes"]["title"],
            "MLO transport and release observations",
        )
        es.request(
            "PUT",
            "/_security/user/fixture-operator",
            {"password": "fixture-operator", "roles": ["mlo_observation_operator"]},
        )
        operator = "fixture-operator:fixture-operator"
        self.assertEqual(
            kibana(
                "/s/observations/api/saved_objects/dashboard/mlo-observations-v02",
                credential=operator,
            ).returncode,
            0,
        )
        self.assertNotEqual(
            kibana(
                "/s/observations/api/saved_objects/dashboard/mlo-observations-v02",
                method="PUT",
                body={"attributes": {"title": "denied"}},
                credential=operator,
            ).returncode,
            0,
        )
        es.request(
            "PUT",
            "/_security/user/fixture-reader",
            {"password": "fixture-reader", "roles": ["mlo_logs_reader"]},
        )
        from mlo_runtime.catalog import Elastic
        from mlo_runtime.operations import UnsafeTransition

        reader = Elastic(
            es.url, str(lab.folder / "ca.crt"), "fixture-reader", "fixture-reader"
        )
        self.assertGreaterEqual(
            reader.request("GET", "/logs-mlo.*-*/_count")["count"], len(expected)
        )
        with self.assertRaises(UnsafeTransition):
            reader.request("GET", "/venues-private/_search")
        (lab.folder / "acceptance.json").write_text(
            json.dumps(
                {
                    "filebeat_logstash_es_kibana": "pass",
                    "mtls_reject": "pass",
                    "dedupe_pii": "pass",
                    "collector_outage_registry": "pass",
                    "es_outage_persistent_queue": "pass",
                    "synthetic_projection_cursor_ledger": "pass",
                    "silent_heartbeat": "pass",
                    "dashboard_import_rbac": "pass",
                    "real_app_mongo_projection": "not_run",
                    "ready_for_aws": False,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    unittest.main()
