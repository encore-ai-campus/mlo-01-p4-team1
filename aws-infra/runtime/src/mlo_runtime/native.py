"""Render the native/systemd redesign for review; never installs or starts it.

Separate from the legacy Compose installer until native host acceptance exists.
The contract and units are deliberately not an executable deployment artifact.
"""
from copy import deepcopy
import argparse
import json
import hashlib
import re
from pathlib import Path

import yaml

from .contracts import ROOT, load_inventory
from .render import service_configs, stable_id, compose, haproxy


def contract():
    value = json.loads((ROOT / "aws-infra/contracts/native-host.v1.json").read_text())
    if (value.get("profile") != "native-data-systemd-v1"
            or value.get("ready_for_aws") is not False
            or set(value.get("native_roles", [])) != {"kafka", "spark", "elasticsearch"}):
        raise ValueError("native design contract changed or claims unverified readiness")
    return value


def unit(name, command, data, *, generation, memory, cpu="100%", environment=None, credentials=(), prestart=()):
    # Readiness and exact mount UUID are accepted by the future installer. This
    # preview must not be enabled merely because systemctl reports 'active'.
    return (
        f"[Unit]\nDescription=MLO native {name} ({generation})\n"
        "After=network-online.target\nWants=network-online.target\n"
        f"RequiresMountsFor=/srv/mlo {data}\n"
        "ConditionPathExists=/etc/mlo-native/accepted-host.json\n"
        "[Service]\nType=exec\nUser=mlo\nGroup=mlo\nUMask=0077\n"
        "ExecStartPre=/usr/bin/mountpoint -q /srv/mlo\n"
        f"ExecStartPre=+/usr/bin/python3 /opt/mlo/native/helpers/{hashlib.sha256(Path(__file__).with_name('native_mount.py').read_bytes()).hexdigest()}/native_mount.py /etc/mlo/host-binding.json\n"
        + "".join("ExecStartPre=+" + cmd + "\n" for cmd in prestart)
        + "".join(f"LoadCredential={s}:/run/mlo/secrets/{s}\n" for s in credentials)
        + "".join(f'Environment="{k}={v}"\n' for k, v in (environment or {}).items())
        + f"WorkingDirectory={data}\nExecStart={command}\n"
        "Restart=on-failure\nRestartSec=5s\nTimeoutStopSec=120s\n"
        "KillSignal=SIGTERM\nKillMode=control-group\nSendSIGKILL=yes\n"
        f"CPUAccounting=yes\nCPUQuota={cpu}\nMemoryAccounting=yes\nMemoryMax={memory}\n"
        "LimitNOFILE=65535\nTasksMax=512\nNoNewPrivileges=yes\n"
        "ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
        f"ReadWritePaths={data}\nStandardOutput=journal\nStandardError=journal\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def plan(inventory, revision="review"):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", revision):
        raise ValueError("invalid native configuration revision")
    spec = contract()
    from .native_releases import lock
    releases = lock(ROOT / "aws-infra/runtime/native-releases.lock.json")["releases"]
    from .native_connectors import locked, lock_sha256, release_id, classpath
    connector_lock = ROOT / "aws-infra/runtime/native-connectors.lock.json"
    locked(connector_lock)  # fail closed if extra/distribution closure changes
    connector_digest = lock_sha256(connector_lock)
    spark_runtime_id = release_id(releases["spark"]["digest"], connector_digest)
    connector_classpath = classpath(spec["installation"]["prefix"], spark_runtime_id)
    guard_sha = hashlib.sha256(Path(__file__).with_name("native_connector_guard.py").read_bytes()).hexdigest()
    homes = {name: spec["installation"]["prefix"] + "/releases/" + name + "-" + item["digest"][:16] for name, item in releases.items()}
    helper_hash = hashlib.sha256(Path(__file__).with_name("native_launch.py").read_bytes()).hexdigest()
    launcher = spec["installation"]["prefix"] + "/helpers/" + helper_hash + "/native_launch.py"
    nodes = {}
    prefix = spec["installation"]["prefix"]
    generation = inventory["generation"]
    for node in inventory["nodes"]:
        role = node["role"]
        if role not in spec["native_roles"]:
            continue
        base = f"/srv/mlo/{generation}"
        config = f"/etc/mlo-native/{generation}/{node['name']}/{revision}"
        files = service_configs(node, inventory)
        units, configs = {}, {}
        if role == "kafka":
            name = "mlo-kafka.service"
            credential = f"/run/credentials/{name}"
            configs["server.properties"] = files["server.properties"].replace(
                "/var/lib/kafka/data", base + "/kafka"
            ).replace("/run/mlo/secrets", credential)
            configs["health.properties"] = configs["server.properties"] + "security.protocol=SSL\n"
            units[name] = unit("Kafka", f"{homes['kafka']}/bin/kafka-server-start.sh {config}/server.properties",
                base + "/kafka", generation=generation, memory="2G",
                environment={"JAVA_HOME": homes["java21"], "KAFKA_HEAP_OPTS": "-Xms768m -Xmx768m", "LOG_DIR": base + "/kafka/logs"},
                credentials=("kafka-keystore.p12", "cluster-truststore.p12", "kafka-passwords.properties"))
        elif role == "elasticsearch":
            name = "mlo-elasticsearch.service"
            credential = f"/run/credentials/{name}"
            es = yaml.safe_load(files["elasticsearch.yml"])
            es.update({"path.data": base + "/elasticsearch", "path.logs": base + "/elasticsearch/logs"})
            for key, value in list(es.items()):
                if isinstance(value, str) and value.startswith("secrets/"):
                    es[key] = credential + "/" + value.split("/", 1)[1]
                elif isinstance(value, list):
                    es[key] = [credential + "/" + x.split("/", 1)[1]
                               if isinstance(x, str) and x.startswith("secrets/") else x for x in value]
            configs["elasticsearch.yml"] = yaml.safe_dump(es, sort_keys=False)
            es["xpack.security.autoconfiguration.enabled"] = False
            configs["elasticsearch.yml"] = yaml.safe_dump(es, sort_keys=False)
            units[name] = unit("Elasticsearch", homes["elasticsearch"] + "/bin/elasticsearch",
                base + "/elasticsearch", generation=generation, memory="5G",
                environment={"ES_PATH_CONF": config, "ES_JAVA_OPTS": "-Xms2g -Xmx2g"},
                credentials=("server.key", "server.crt", "ca.crt"))
        else:
            configs["zoo.cfg"] = files["zoo.cfg"].replace("/data/zookeeper", base + "/zookeeper").replace(
                "/run/mlo/secrets", "/run/credentials/mlo-zookeeper.service")
            configs["myid"] = files["myid"]
            configs["zoo.cfg"] += "admin.enableServer=false\n4lw.commands.whitelist=ruok,mntr,srvr\n"
            units["mlo-zookeeper.service"] = unit("ZooKeeper", homes["zookeeper"] + "/bin/zkServer.sh start-foreground " + config + "/zoo.cfg",
                base + "/zookeeper", generation=generation, memory="512M",
                environment={"JAVA_HOME": homes["java21"], "JVMFLAGS": "-Xms128m -Xmx256m", "ZOO_LOG_DIR": base + "/zookeeper/logs"},
                credentials=("zookeeper-keystore.p12", "cluster-truststore.p12", "keystore-password", "truststore-password"))
            masters = ",".join(n["private_ip"] + ":7077" for n in sorted(inventory["nodes"], key=lambda n: n["name"]) if n["role"] == "spark")
            for component in ("master", "worker"):
                name = f"mlo-spark-{component}.service"
                # Separate conf directories resolve each unit's credential path.
                conf = files["spark-defaults.conf"].replace("/run/mlo/secrets", f"/run/credentials/{name}")
                conf += "spark.ssl.keyStoreType PKCS12\nspark.ssl.trustStoreType PKCS12\nspark.ssl.standalone.needClientAuth true\nspark.ssl.standalone.port " + ("8443" if component == "master" else "8444") + "\n"
                if any(line.split(None, 1)[0] in {"spark.jars.packages", "spark.jars.repositories", "spark.jars.ivy"} for line in conf.splitlines() if line.strip() and not line.lstrip().startswith("#")):
                    raise ValueError("runtime Spark package resolution is forbidden")
                conf += "spark.driver.extraClassPath " + connector_classpath + "\n"
                conf += "spark.executor.extraClassPath " + connector_classpath + "\n"
                configs[f"{component}/spark-defaults.conf"] = conf
                command = f"/usr/bin/python3 {launcher} {component} {homes['spark']} {config}/{component} --host {node['private_ip']} --port {7077 if component == 'master' else 7078} --webui-port {8080 if component == 'master' else 8081}"
                if component == "worker":
                    command += f" --cores 1 --memory 3g --work-dir {base}/spark/work spark://{masters}"
                guard_cmd = (f"/usr/bin/python3 {prefix}/helpers/{guard_sha}/native_connector_guard.py "
                             f"/etc/mlo/host-binding.json {config}/native-connectors.lock.json "
                             f"{config}/native-releases.lock.json {homes['spark']} "
                             f"{prefix}/connectors/spark-{spark_runtime_id} {spark_runtime_id}")
                units[name] = unit("Spark " + component, command, base + "/spark", generation=generation,
                    memory="1G" if component == "master" else "5G", prestart=(guard_cmd,),
                    environment={"JAVA_HOME": homes["java21"], "SPARK_CONF_DIR": config + "/" + component,
                                 "SPARK_LOG_DIR": base + "/spark/logs", "SPARK_DAEMON_MEMORY": "512m"},
                    credentials=("spark-auth", "spark-keystore.p12", "cluster-truststore.p12", "keystore-password", "truststore-password"))
        nodes[node["name"]] = {
            "node": node["name"], "generation": inventory["generation"], "environment": inventory["environment"],
            "role": role, "execution_target": "native-systemd",
            "revision": revision, "private_ip": node["private_ip"],
            "releases": {key: releases[key] for key in ({"kafka", "java21"} if role == "kafka" else {"elasticsearch"} if role == "elasticsearch" else {"spark", "zookeeper", "java21"})},
            "helper_sha256": helper_hash,
            "node_id": stable_id(node) if role in {"kafka", "spark"} else node["name"],
            "persistent_mount": "/srv/mlo", "config_directory": config,
            "units": units, "configs": configs,
            "ready_for_aws": False,
        }
        if role == "spark":
            nodes[node["name"]].update({"native_connector_lock_sha256": connector_digest,
                                        "spark_runtime_release_id": spark_runtime_id,
                                        "connector_classpath": connector_classpath})
        helper_compose = compose(node, inventory)
        helper_compose["services"] = {k: v for k, v in helper_compose["services"].items() if k in {"host-proxy", "filebeat"}}
        for service in helper_compose["services"].values():
            service["volumes"] = [v.replace("/etc/mlo/current/config", config) for v in service["volumes"]]
        nodes[node["name"]]["auxiliary_compose"] = helper_compose
        configs["haproxy.cfg"] = haproxy(inventory)
        configs["filebeat.yml"] = files["filebeat.yml"]
    return {"profile": spec["profile"], "generation": generation,
            "native_connector_lock_sha256": connector_digest,
            "spark_runtime_release_id": spark_runtime_id,
            "example_only": inventory["example_only"], "ready_for_aws": False,
            "deployment_blockers": deepcopy(spec["deployment_blockers"]), "nodes": nodes,
            "control_drivers": "native fenced units required; legacy Docker driver adapter is not native stop evidence"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory, _ = load_inventory(args.inventory)
    if not inventory["example_only"] and ".private" not in args.output.resolve().parts:
        raise ValueError("real native host plan must remain under .private")
    value = plan(inventory)
    # Immutable review output, never overwrite an earlier plan or deploy.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    print("Native/systemd design rendered; ready_for_aws=false; no installation or service execution")


if __name__ == "__main__":
    main()
