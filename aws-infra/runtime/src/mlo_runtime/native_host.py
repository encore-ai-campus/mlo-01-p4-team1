"""Host-bound native data installer. Stdlib-only and offline; never formats EBS.

install stages verified binaries/configs without starting services. deploy starts
the accepted units; health checks protocol responses. rollback changes configs
only for the identical native release set after an externally reviewed gate.
"""
import argparse
import hashlib
import json
import os
import pwd
import grp
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import datetime
import uuid

from .install import (InstallError, atomic_json, deployment_lock, host_path, protected_binding,
                      read, require, sha, validate_identity, ComposeSystemdBackend)
from .native_releases import archive_path, extract, file_digest, lock
from .native_connectors import (locked as connector_lock, release_id as spark_runtime_id,
                                classpath as connector_classpath, stage_bundle, verify_bundle)


class NativeHost:
    def __init__(self, binding, guard=None):
        self.binding, self.guard = binding, guard

    def path(self, value):
        return host_path(self.binding, value)

    def command(self, argv, *, timeout=180):
        if self.guard is not None:
            self.guard.check()
        try:
            result = subprocess.run(argv, check=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            # Do not echo commands/output containing TLS credentials or paths.
            raise InstallError("native controlled command failed") from exc
        if self.guard is not None:
            self.guard.check()
        return result.stdout

    def mount(self):
        observer = ComposeSystemdBackend(self.binding)
        observer.command = self.command
        observer.check_mount(self.path("/srv/mlo"))

    def preflight(self):
        if self.binding.get("lab") is True:
            return
        os_release = self.path("/etc/os-release").read_text()
        require('ID=ubuntu' in os_release and 'VERSION_ID="24.04"' in os_release, "native Ubuntu 24.04 required")
        require(pwd.getpwnam("mlo").pw_uid == 2000 and grp.getgrnam("mlo").gr_gid == 2000, "native dedicated uid/gid required")
        controllers = self.path("/sys/fs/cgroup/cgroup.controllers").read_text().split()
        require({"cpu", "memory"} <= set(controllers), "native cgroup v2 accounting required")
        self.command(["systemctl", "show", "--property=Version", "--no-pager"])
        if self.binding["role"] == "elasticsearch":
            require(int(self.path("/proc/sys/vm/max_map_count").read_text()) >= 262144, "ES vm.max_map_count requires accepted host bootstrap")

    def stopped(self, units):
        for unit in units:
            raw = self.command(["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,MainPID,ControlGroup,KillMode", "--no-pager"]).decode()
            props = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            require(props.get("LoadState") == "loaded" and props.get("ActiveState") == "inactive"
                    and props.get("MainPID") == "0" and props.get("KillMode") == "control-group", "native unit has not stopped")
            group = props.get("ControlGroup", "")
            require(not group or re.fullmatch(r"/system.slice/[A-Za-z0-9_.@-]+\.service", group), "native cgroup path invalid")
            if group:
                root = self.path("/sys/fs/cgroup" + group)
                if root.exists():
                    for p in root.rglob("cgroup.procs"):
                        require(not p.is_symlink() and not p.read_text().strip(), "native descendants still alive")
                    events = root / "cgroup.events"
                    require(events.exists() and "populated 0" in events.read_text().splitlines(), "native cgroup remains populated")

    def stop(self, node):
        # Stop writers before coordination; do not stop another service by name input.
        names = sorted(node["units"], key=lambda n: ("zookeeper" in n, "master" in n))
        for name in names:
            self.command(["systemctl", "stop", name])
        self.stopped(names)

    def helpers(self, node, operation):
        helpers = node["auxiliary_compose"]
        require(set(helpers["services"]) == {"host-proxy", "filebeat"}, "only proxy/observation auxiliary containers allowed")
        require(self.command(["docker", "compose", "version", "--short"]).decode().strip() == "5.5.1", "accepted helper Compose required")
        config = self.path(node["config_directory"] + "/auxiliary-compose.json")
        argv = ["docker", "compose", "--project-name", "mlo-native-helpers-" + node["node"], "--file", str(config)]
        if operation == "start":
            self.command([*argv, "up", "-d", "--pull", "never", "--remove-orphans"])
        elif operation == "health":
            rows = self.command([*argv, "ps", "--all", "--format", "json"]).decode().strip()
            records = json.loads(rows) if rows.startswith("[") else [json.loads(line) for line in rows.splitlines()]
            require({r["Service"] for r in records} == {"host-proxy", "filebeat"}
                    and all(r.get("State") == "running" and r.get("Health") == "healthy" for r in records), "native observation/proxy helpers unhealthy")
        else:
            raise InstallError("invalid helper operation")

    def connectors(self, node):
        if node["role"] != "spark":
            return None
        connector_lock_path = self.path(node["config_directory"] + "/native-connectors.lock.json")
        require(file_digest(connector_lock_path) == node["native_connector_lock_sha256"],
                "native installed connector lock changed")
        spark_item = node["releases"]["spark"]
        spark_home = self.path("/opt/mlo/native/releases/spark-" + spark_item["digest"][:16])
        accepted = verify_bundle(spark_home, self.path("/opt/mlo/native"), connector_lock_path,
                                 spark_item["digest"], expected_id=node["spark_runtime_release_id"])
        expected_cp = (node["connector_classpath"] if self.binding.get("root", "/") == "/"
                       else connector_classpath(self.path("/opt/mlo/native"), node["spark_runtime_release_id"]))
        require(accepted["connector_classpath"] == expected_cp,
                "native installed connector classpath changed")
        return accepted

    def start(self, node):
        self.connectors(node)
        for name in sorted(node["units"], key=lambda n: ("zookeeper" not in n, "worker" in n)):
            self.command(["systemctl", "start", name])

    def health(self, node):
        self.connectors(node)
        for name in node["units"]:
            self.command(["systemctl", "is-active", "--quiet", name], timeout=10)
        self.helpers(node, "health")
        ip = node["private_ip"]
        secrets = self.path("/run/mlo/secrets")
        if node["role"] == "kafka":
            item = node["releases"]["kafka"]
            binary = self.path("/opt/mlo/native/releases/kafka-" + item["digest"][:16] + "/bin/kafka-broker-api-versions.sh")
            output = self.command(["/usr/bin/env", "JAVA_HOME=" + str(self.path("/opt/mlo/native/releases/java21-" + node["releases"]["java21"]["digest"][:16])), str(binary), "--bootstrap-server", ip + ":9093", "--command-config", str(self.path(node["config_directory"] + "/health.properties"))], timeout=30).decode()
            require("ApiVersions(" in output or "Produce(" in output, "Kafka authenticated protocol response missing")
            return {"protocol": "kafka-tls", "ready": True}
        base = ["curl", "--fail", "--silent", "--show-error", "--noproxy", "*", "--max-time", "10", "--cacert", str(secrets / "ca.crt")]
        if node["role"] == "elasticsearch":
            response = json.loads(self.command([*base, "--netrc-file", str(secrets / "es-netrc"), f"https://{ip}:9200/_cluster/health?wait_for_status=yellow&timeout=5s"]))
            require(response.get("timed_out") is False and response.get("status") in {"green", "yellow"}
                    and response.get("number_of_nodes") == 3, "ES quorum health missing")
            cluster = json.loads(self.command([*base, "--netrc-file", str(secrets / "es-netrc"), f"https://{ip}:9200/"]))
            expected = self.binding.get("cluster_identity", {}).get("cluster_uuid")
            require(cluster.get("cluster_uuid") and (self.binding.get("initial_bootstrap") is True or cluster["cluster_uuid"] == expected), "ES cluster UUID mismatch")
            return {"protocol": "es-tls", "ready": True, "cluster_uuid": cluster["cluster_uuid"]}
        responses = []
        for port in (8443, 8444):
            responses.append(json.loads(self.command([*base, "--cert", str(secrets / "client.crt"), "--key", str(secrets / "client.key"), f"https://{ip}:{port}/json/"])))
        master, worker = responses
        require(master.get("status") in {"ALIVE", "STANDBY"} and isinstance(master.get("workers"), list), "Spark master/quorum response missing")
        require(worker.get("masterurl", "").startswith("spark://") and worker.get("cores") == 1 and isinstance(worker.get("executors"), list), "Spark worker not connected with accepted cores")
        return {"protocol": "spark-mtls", "ready": True, "master_status": master["status"]}


def verify_payload(payload, binding, operation):
    payload = Path(payload)
    manifest = read(payload / "manifest.json")
    require(sha(payload / "manifest.json") in binding.get("accepted_manifests", []), "native manifest not accepted")
    require(not manifest.get("fixture", False) or binding.get("lab") is True, "native fixture cannot execute on AWS")
    from .native_releases import safe_path
    for name, expected in manifest["files"].items():
        require(safe_path(name), "unsafe native payload path")
        target = payload / name
        require(not any(p.is_symlink() for p in [target, *target.parents] if p != payload.parent), "native payload link")
        require(target.is_file() and sha(target) == expected, "native payload checksum mismatch")
    actual = {str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file()}
    require(actual == set(manifest["files"]) | {"manifest.json"}, "native payload coverage mismatch")
    require(manifest.get("schema_version") == "1.0.0" and manifest.get("platform") == "linux/amd64"
            and manifest.get("example_only") is False and operation in manifest.get("operations", []), "native artifact type/operation mismatch")
    require((manifest["environment"], manifest["generation"], manifest["node_roles"].get(binding["node"])) == (binding["environment"], binding["generation"], binding["role"]), "native payload target mismatch")
    node = read(payload / "hosts" / binding["node"] / "native.json")
    require((node["node"], node["environment"], node["generation"], node["role"]) == (binding["node"], binding["environment"], binding["generation"], binding["role"]), "native config binding mismatch")
    require(node["revision"] == manifest["artifact_version"] and re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", node["revision"]), "native revision mismatch")
    require(node["config_directory"] == f"/etc/mlo-native/{binding['generation']}/{binding['node']}/{node['revision']}", "native config path mismatch")
    releases_path = payload / "aws-infra/runtime/native-releases.lock.json"
    require(file_digest(releases_path) == binding.get("native_release_lock_sha256"), "native release lock not accepted")
    accepted = lock(releases_path)["releases"]
    require(all(accepted.get(k) == v for k, v in node["releases"].items()), "native release closure mismatch")
    connector_path = payload / "aws-infra/runtime/native-connectors.lock.json"
    connector_lock(connector_path)
    connector_sha = file_digest(connector_path)
    require(connector_sha == binding.get("native_connector_lock_sha256"),
            "native connector lock not accepted")
    expected_id = spark_runtime_id(accepted["spark"]["digest"], connector_sha)
    if node["role"] == "spark":
        cp = connector_classpath("/opt/mlo/native", expected_id)
        require(node.get("native_connector_lock_sha256") == connector_sha
                and node.get("spark_runtime_release_id") == expected_id
                and node.get("connector_classpath") == cp, "native Spark connector identity mismatch")
        for component in ("master", "worker"):
            props = {}
            for line in node["configs"][component + "/spark-defaults.conf"].splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    key, val = line.split(None, 1)
                    require(key not in props, "native Spark configuration duplicate key")
                    props[key] = val
            require(props.get("spark.driver.extraClassPath") == cp
                    and props.get("spark.executor.extraClassPath") == cp
                    and not ({"spark.jars.packages", "spark.jars.repositories", "spark.jars.ivy"} & set(props)),
                    "native Spark locked classpath mismatch")
    require(set(manifest["image_digests"].values()) <= set(binding.get("image_digests", {}).values()), "native auxiliary images not accepted")
    require(set(node["auxiliary_compose"]["services"]) == {"host-proxy", "filebeat"}
            and all(s["image"] in manifest["image_digests"].values() for s in node["auxiliary_compose"]["services"].values()), "native auxiliary service/image mismatch")
    require(set(manifest["required_secrets"][binding["node"]]) == set(binding.get("required_secrets", [])), "native secret binding mismatch")
    for name in binding["required_secrets"]:
        require(re.fullmatch(r"/run/mlo/secrets/[A-Za-z0-9_.-]+", name), "native secret path")
        secret = host_path(binding, name)
        require(secret.is_file() and secret.stat().st_size > 0 and stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0, "native secret missing/protection")
        require(binding.get("lab") is True or secret.stat().st_uid == 0, "native secret owner")
    for group, hashes in (("configs", "config_hashes"), ("units", "unit_hashes")):
        require(set(node[group]) == set(node[hashes]) and all(hashlib.sha256(b.encode()).hexdigest() == node[hashes][p] for p, b in node[group].items()), "native config/unit checksum")
    return manifest, node


def stage(payload, node, backend):
    binding = backend.binding
    base = backend.path("/srv/mlo/" + binding["generation"])
    directories = ["kafka"] if node["role"] == "kafka" else ["elasticsearch"] if node["role"] == "elasticsearch" else ["spark", "zookeeper"]
    for name in directories:
        path = base / name
        path.mkdir(parents=True, exist_ok=True, mode=0o750)
        # Initial empty directories may be owned once; never recursively change persisted files.
        if not any(path.iterdir()) and path.stat().st_uid == 0:
            os.chown(path, 2000, 2000)
        require(path.stat().st_uid == 2000 and path.stat().st_gid == 2000, "native persisted owner mismatch; inspected migration required")
        for child in (["logs", "work"] if name == "spark" else ["logs"]):
            sub = path / child
            require(not sub.is_symlink(), "native persistent subdirectory link")
            sub.mkdir(exist_ok=True, mode=0o750)
            if not any(sub.iterdir()) and sub.stat().st_uid == 0:
                os.chown(sub, 2000, 2000)
            require(sub.stat().st_uid == 2000 and sub.stat().st_gid == 2000, "native persistent subdirectory owner mismatch")
    validate_identity(node["role"], node["node"], base, binding.get("cluster_identity", {}))
    if node["role"] == "spark":
        os.chown(base / "zookeeper/myid", 2000, 2000)
    if node["role"] == "elasticsearch":
        require(type(binding.get("initial_bootstrap")) is bool, "explicit ES bootstrap decision required")
        if binding["initial_bootstrap"]:
            require(not (base / "elasticsearch/nodes").exists() and not (base / "elasticsearch/_state").exists(), "ES existing data cannot bootstrap a new cluster")
        else:
            require(bool(binding.get("cluster_identity", {}).get("cluster_uuid")), "ES retained cluster UUID required")
    prefix = backend.path("/opt/mlo/native")
    prefix.mkdir(parents=True, exist_ok=True, mode=0o755)
    require(os.access(prefix, os.X_OK), "native installation root not traversable")
    cache = backend.path("/var/lib/mlo/native-archives")
    for name, item in node["releases"].items():
        destination = prefix / "releases" / (name + "-" + item["digest"][:16])
        marker = destination / ".mlo-native-receipt.json"
        if destination.exists():
            require(marker.is_file() and read(marker).get("publisher_digest") == item["digest"], "unmanaged native release exists")
        else:
            extract(archive_path(cache, name, item), item, destination)
            atomic_json(marker, {"publisher_digest": item["digest"], "version": item["version"]})
    if node["role"] == "spark":
        spark_item = node["releases"]["spark"]
        spark_home = prefix / "releases" / ("spark-" + spark_item["digest"][:16])
        connector_lock_path = Path(payload) / "aws-infra/runtime/native-connectors.lock.json"
        installed = stage_bundle(spark_home, backend.path("/var/lib/mlo/native-connectors"),
                                 prefix, connector_lock_path, spark_item["digest"])
        require(installed["spark_runtime_release_id"] == node["spark_runtime_release_id"]
                and installed["connector_classpath"] == node["connector_classpath"],
                "native staged connector closure mismatch")
    for helper in ("native_launch.py", "native_mount.py", "native_connector_guard.py"):
        source = Path(payload) / "aws-infra/runtime/src/mlo_runtime" / helper
        destination = prefix / "helpers" / sha(source) / helper
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if destination.exists():
            require(sha(destination) == sha(source), "native helper changed")
        else:
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
    for name, uid in (("spool", 0), ("filebeat", 0)):
        path = base / "observations" / name
        require(not path.is_symlink(), "native observation persistence link")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(path.stat().st_uid == uid, "native observation persistence owner mismatch")
    config = backend.path(node["config_directory"])
    require(not config.exists(), "native config revision exists; use existing accepted stage")
    config.mkdir(parents=True, mode=0o750)
    if node["role"] == "spark":
        trusted_lock = Path(payload) / "aws-infra/runtime/native-connectors.lock.json"
        (config / "native-connectors.lock.json").write_bytes(trusted_lock.read_bytes())
        (config / "native-releases.lock.json").write_bytes(
            (Path(payload) / "aws-infra/runtime/native-releases.lock.json").read_bytes())
    # Archive-provided ES JVM/log4j defaults are required even when yml is replaced.
    if node["role"] == "elasticsearch":
        home = prefix / "releases" / ("elasticsearch-" + node["releases"]["elasticsearch"]["digest"][:16])
        shutil.copytree(home / "config", config, dirs_exist_ok=True)
    for name, body in node["configs"].items():
        require(not name.startswith("/") and ".." not in Path(name).parts, "native config path")
        if name == "elasticsearch.yml" and not binding.get("initial_bootstrap"):
            body = "\n".join(line for line in body.splitlines() if not line.startswith("cluster.initial_master_nodes:")) + "\n"
            # ES YAML stores initial masters as a list; drop the list before writing below.
            import_marker = "cluster.initial_master_nodes:"
            original = node["configs"][name].splitlines()
            kept, removing = [], False
            for line in original:
                if line.startswith(import_marker):
                    removing = True
                    continue
                if removing and (line.startswith("- ") or line.startswith("  - ")):
                    continue
                removing = False
                kept.append(line)
            body = "\n".join(kept) + "\n"
        path = config / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        path.write_text(body)
    (config / "auxiliary-compose.json").write_text(json.dumps(node["auxiliary_compose"], sort_keys=True))
    for p in [config, *config.rglob("*")]:
        require(not p.is_symlink(), "native config link")
        os.chown(p, 0, 2000)
        p.chmod(0o750 if p.is_dir() else 0o640)
    units = config / "units"
    units.mkdir(mode=0o750)
    for name, body in node["units"].items():
        require(re.fullmatch(r"mlo-(kafka|elasticsearch|zookeeper|spark-master|spark-worker)\.service", name), "native service name")
        (units / name).write_text(body)
        (units / name).chmod(0o644)
    # Parent traversal is required by the non-login service user; other runtime
    # releases stay 0700 and secret directories stay root-only.
    for parent in ("/opt/mlo", "/etc/mlo-native", f"/etc/mlo-native/{binding['generation']}", f"/etc/mlo-native/{binding['generation']}/{binding['node']}"):
        p = backend.path(parent)
        p.mkdir(parents=True, exist_ok=True)
        p.chmod(0o755)
    return config


def activate(node, backend):
    target = backend.path("/etc/systemd/system")
    target.mkdir(parents=True, exist_ok=True)
    for name, body in node["units"].items():
        require(re.fullmatch(r"mlo-(kafka|elasticsearch|zookeeper|spark-master|spark-worker)\.service", name), "native unit name")
        p = target / name
        require(not p.is_symlink() and (not p.exists() or p.read_text().startswith("[Unit]\nDescription=MLO native ")), "unmanaged unit collision")
        temporary = target / ("." + name + ".pending")
        with temporary.open("x") as stream:
            stream.write(body)
        temporary.chmod(0o644)
        os.replace(temporary, p)
    backend.command(["systemctl", "daemon-reload"])
    backend.command(["systemd-analyze", "verify", *[str(target / n) for n in node["units"]]])
    atomic_json(backend.path("/etc/mlo-native/accepted-host.json"), {"node": node["node"], "generation": node["generation"], "revision": node["revision"], "mount_uuid": backend.binding.get("mount_uuid"), "ready_for_aws": False})


def require_change_evidence(binding, guard):
    """Configuration rollback never substitutes for a verified data migration."""
    from infra_common.coordination import require_operation
    from infra_common.evidence import validate_shutdown
    token = require_operation(guard, "deploy")
    def protected(name):
        path = host_path(binding, name)
        require(path.is_file() and path.stat().st_mode & 0o077 == 0
                and (binding.get("lab") is True or path.stat().st_uid == 0), "native change evidence protection")
        return path
    proof = protected(binding["native_change_evidence_path"])
    require(sha(proof) == binding.get("native_change_evidence_sha256"), "native change proof checksum")
    inventory = read(protected(binding["native_change_inventory_path"]))
    issuers = {name: protected(path).read_bytes() for name, path in binding["native_change_issuers"].items()}
    validate_shutdown(read(proof), inventory, token, issuers)


def emit_event(receipt, backend):
    """Fixed sanitized status only; never harvest raw journal or CLI output."""
    folder = backend.path("/srv/mlo/" + receipt["generation"] + "/observations/spool")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    event = {"@timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": {"id": str(uuid.uuid4()), "dataset": "mlo.service", "action": "native-" + receipt["operation"],
                  "outcome": "success" if receipt["status"] == "success" else "failure"},
        "service": {"name": "native-runtime"}, "host": {"name": receipt["node"]},
        "pipeline": {"name": "native-runtime", "run_id": str(uuid.uuid4()), "stage": receipt["operation"],
                     "state": "SUCCEEDED" if receipt["status"] == "success" else "FAILED", "attempt": 1}}
    body = (json.dumps(event, sort_keys=True) + "\n").encode()
    with deployment_lock(folder / "append.lock"):
        files = sorted(folder.glob("*.ndjson"))
        require(not any(p.is_symlink() for p in files), "native observation spool link")
        require(sum(p.stat().st_size for p in files) + len(body) <= 64 * 1024 * 1024, "native observation spool full; archive required")
        current = files[-1] if files else folder / "00000000.ndjson"
        if current.exists() and current.stat().st_size + len(body) > 4 * 1024 * 1024:
            require(len(files) < 16, "native observation spool file bound")
            current = folder / f"{int(current.stem) + 1:08d}.ndjson"
        fd = os.open(current, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())


def execute(payload, binding_path, operation, environment, generation, node_name, *, backend=None, guard=None):
    binding = protected_binding(binding_path, native=True)
    require((environment, generation, node_name) == (binding["environment"], binding["generation"], binding["node"]), "native execution identity mismatch")
    if binding.get("lab") is not True:
        require(guard is not None, "native common deployment guard required")
        from infra_common.coordination import require_operation
        token = require_operation(guard, "deploy")
        require(token["scope"]["generation"] == generation and token["scope"]["environment"] == environment, "native common guard scope")
    backend = backend or NativeHost(binding, guard)
    manifest, node = verify_payload(payload, binding, operation)
    state_path = backend.path("/var/lib/mlo/native-state.json")
    receipt = {"operation": operation, "node": node_name, "generation": generation, "artifact_version": manifest["artifact_version"], "status": "failed", "changed": False, "ready_for_aws": False}
    mount_verified = False
    with deployment_lock(backend.path("/var/lib/mlo/native.lock")):
        try:
            backend.mount()
            mount_verified = True
            backend.preflight()
            state = read(state_path) if state_path.exists() else {"current": None, "previous": None, "staged": {}}
            require(not state.get("pending"), "native crash/pending transition requires independent reconciliation")
            if operation == "install":
                if node["revision"] not in state["staged"]:
                    stage(payload, node, backend)
                    state["staged"][node["revision"]] = node
                    atomic_json(state_path, state)
                    receipt["changed"] = True
            elif operation == "health":
                require(state["current"] == node["revision"], "native health revision mismatch")
                receipt["health"] = backend.health(state["staged"][state["current"]])
            elif operation in {"deploy", "rollback"}:
                revision = node["revision"] if operation == "deploy" else state["previous"]
                require(revision in state["staged"], "native revision not staged")
                selected = state["staged"][revision]
                old = state["staged"].get(state["current"])
                require(selected["releases"] == node["releases"]
                        and selected.get("spark_runtime_release_id") == node.get("spark_runtime_release_id")
                        and selected.get("native_connector_lock_sha256") == node.get("native_connector_lock_sha256"),
                        "native binary/connector downgrade requires separate migration")
                if old and old["revision"] == revision:
                    receipt["health"] = backend.health(old)
                    receipt["status"] = "success"
                    return receipt
                if old:
                    require(binding.get("planned_change_accepted") is True, "native planned stop/backup gate required")
                    require_change_evidence(binding, guard)
                atomic_json(state_path, {**state, "pending": {"operation": operation, "revision": revision}})
                if old:
                    backend.stop(old)
                try:
                    activate(selected, backend)
                    backend.helpers(selected, "start")
                    backend.start(selected)
                    deadline = time.monotonic() + 120
                    while True:
                        try:
                            receipt["health"] = backend.health(selected)
                            break
                        except (InstallError, ValueError):
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(2)
                    for name in selected["units"]:
                        backend.command(["systemctl", "enable", name])
                    state.update({"current": revision, "previous": old["revision"] if old else None, "pending": None})
                    atomic_json(state_path, state)
                    receipt["changed"] = True
                except BaseException:
                    backend.stop(selected)
                    # Never automatically restart a previous writer or hide a failed migration.
                    # Explicit reviewed rollback is available only for identical binaries.
                    raise InstallError("native activation failed; services stopped; reconcile pending state before rollback")
            else:
                raise InstallError("invalid native operation")
            receipt["status"] = "success"
            return receipt
        finally:
            atomic_json(backend.path("/var/lib/mlo/native-receipts/" + str(time.time_ns()) + ".json"), receipt)
            if mount_verified:
                emit_event(receipt, backend)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["install", "deploy", "health", "rollback"], required=True)
    for name in ("environment", "generation", "node"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--binding", default="/etc/mlo/host-binding.json")
    args = parser.parse_args()
    payload = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(payload / "aws-infra/contracts"))
    try:
        binding = protected_binding(args.binding, native=True)
        from infra_common.factory import load_guard
        guard = load_guard(binding["coordination_contract"], binding["coordination_scope"], "native-runtime-" + args.node, "deploy")
        with guard.hold():
            result = execute(payload, args.binding, args.operation, args.environment, args.generation, args.node, guard=guard)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError, ImportError):
        print("native runtime refused/failed; inspect protected receipt", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
