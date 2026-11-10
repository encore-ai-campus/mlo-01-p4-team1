"""Native unit/cgroup fencing with the existing authenticated Spark observations.

No Docker command is issued. The common v1 field containers_stopped is retained
as a wire compatibility alias for *execution stopped*, backed by native evidence.
"""
import hashlib
import json
from pathlib import Path
import re
import time

from .execution import DockerSparkExecution
from .lease import valid_token
from .operations import UnsafeTransition
from .operations import atomic_json, local_lock
from .native_host import NativeHost
from .native_connectors import (release_id as spark_runtime_id, classpath as connector_classpath,
                                verify_bundle, lock_sha256)


def identity(token, query):
    valid_token(token)
    if query not in {"weather-stream", "feedback-postgresql", "feedback-analytics-batch", "venues-catalog"} and not re.fullmatch(r"(feedback-analytics-batch|venues-catalog)-[a-f0-9]{16}", query):
        raise UnsafeTransition("unknown native driver query")
    body = json.dumps([token["owner"], token["generation"], token["epoch"], query], separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def verify_driver_connectors(spec, binding, host):
    """Fail closed before registry write/start; no executable can resolve Maven."""
    try:
        from .native_releases import need
        spark_digest = binding["spark_publisher_digest"]
        java_digest = binding["java21_publisher_digest"]
        connector_digest = binding["native_connector_lock_sha256"]
        identifier = spark_runtime_id(spark_digest, connector_digest)
        expected_cp = connector_classpath("/opt/mlo/native", identifier)
        need(spec.get("native_connector_lock_sha256") == connector_digest
             and spec.get("spark_runtime_release_id") == identifier
             and spec.get("connector_classpath") == expected_cp, "driver connector binding mismatch")
        need(spec["spark_home"] == "/opt/mlo/native/releases/spark-" + spark_digest[:16]
             and spec["java_home"] == "/opt/mlo/native/releases/java21-" + java_digest[:16],
             "driver native publisher release mismatch")
        lock_name = binding["native_connector_lock_path"]
        need(lock_name == "/etc/mlo/native-connectors.lock.json", "driver connector lock path not protected")
        lock_file = host.path(lock_name)
        need(lock_file.is_file() and not lock_file.is_symlink()
             and lock_file.stat().st_mode & 0o022 == 0
             and lock_sha256(lock_file) == connector_digest, "driver connector lock protection mismatch")
        if binding.get("lab") is not True:
            need(lock_file.stat().st_uid == 0, "driver connector lock ownership mismatch")
        command = spec["command"]
        forbidden = ("--packages", "--repositories", "--ivy", "--jars", "--conf",
                     "--properties-file", "--driver-class-path", "--driver-library-path",
                     "--archives", "--files", "--driver-java-options")
        need(not any(arg in forbidden or arg.startswith(tuple(flag + "=" for flag in forbidden))
                     or "spark.jars.packages" in arg or "spark.jars.repositories" in arg
                     or "spark.authenticate.secret=" in arg or "spark.ssl.rpc.enabled=false" in arg
                     for arg in command),
             "unreviewed driver dependency/config override or inline authentication secret")
        defaults = host.path(spec["spark_defaults"]).read_text()
        props = {}
        for line in defaults.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split(None, 1)
                need(key not in props, "duplicate driver Spark configuration")
                props[key] = value
        need(props.get("spark.driver.extraClassPath") == expected_cp
             and props.get("spark.executor.extraClassPath") == expected_cp
             and props.get("spark.authenticate") == "true"
             and props.get("spark.ssl.enabled") == "true"
             and props.get("spark.ssl.rpc.enabled") == "true"
             and props.get("spark.network.crypto.saslFallback") == "false"
             and bool(props.get("spark.authenticate.secret.file"))
             and not ({"spark.jars.packages", "spark.jars.repositories", "spark.jars.ivy"} & set(props)),
             "unaccepted native driver encryption/dependency/classpath configuration")
        dependencies = spec.get("application_artifacts", [])
        need(isinstance(dependencies, list) and (dependencies or binding.get("lab") is True),
             "native driver accepted application release missing")
        approved = set()
        for dep in dependencies:
            need(isinstance(dep, dict) and set(dep) == {"path", "sha256"},
                 "native application artifact pin schema")
            name, expected_sha = dep["path"], dep["sha256"]
            need(isinstance(name, str) and name.startswith("/opt/mlo/current/apps/")
                 and ".." not in Path(name).parts
                 and Path(name).suffix in {".zip", ".whl", ".py"}
                 and bool(re.fullmatch(r"[a-f0-9]{64}", expected_sha))
                 and name not in approved, "native application artifact path or digest")
            approved.add(name)
            source = host.path(name)
            need(source.is_file() and not source.is_symlink()
                 and source.stat().st_mode & 0o022 == 0
                 and hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha,
                 "native application artifact changed")
            if binding.get("lab") is not True:
                need(source.stat().st_uid == 0, "native application artifact owner")
        py_indices = [i for i, a in enumerate(command) if a == "--py-files"]
        need(len(py_indices) <= 1
             and not any(a.startswith("--py-files=") for a in command),
             "native application PYTHONPATH override")
        observed = command[py_indices[0] + 1].split(",") if py_indices and py_indices[0] + 1 < len(command) else []
        need(len(observed) == len(approved) and set(observed) == approved,
             "native application dependency closure missing/unreviewed")
        bundle = verify_bundle(host.path(spec["spark_home"]), host.path("/opt/mlo/native"),
                               lock_file, spark_digest, expected_id=identifier)
        actual_cp = (expected_cp if binding.get("root", "/") == "/" else
                     connector_classpath(host.path("/opt/mlo/native"), identifier))
        need(bundle["connector_classpath"] == actual_cp, "driver connector bundle changed")
        # The Control driver and remote PySpark executors may not fall back to
        # the system Python; pin a separately reviewed feedback Python release.
        if binding.get("lab") is not True:
            python_digest = binding["native_feedback_python_lock_sha256"]
            need(bool(re.fullmatch(r"[a-f0-9]{64}", python_digest))
                 and spec.get("native_feedback_python_lock_sha256") == python_digest,
                 "driver Python wheel lock not bound")
            expected_python = ("/opt/mlo/native/python/feedback-" + python_digest[:16]
                               + "/venv/bin/python3.12")
            need(spec.get("python_executable") == expected_python,
                 "driver Python executable not in accepted release")
            python_source = host.path(expected_python)
            need(python_source.is_file() and not python_source.is_symlink()
                 and python_source.stat().st_uid == 0
                 and python_source.stat().st_mode & 0o022 == 0
                 and hashlib.sha256(python_source.read_bytes()).hexdigest()
                 == spec.get("python_executable_sha256"),
                 "accepted Python executable changed")
            py_lock = host.path("/etc/mlo/native-feedback-python.lock.json")
            need(py_lock.is_file() and not py_lock.is_symlink()
                 and py_lock.stat().st_uid == 0
                 and py_lock.stat().st_mode & 0o077 == 0
                 and lock_sha256(py_lock) == python_digest,
                 "protected Python release wheel lock unavailable")
        return identifier
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UnsafeTransition("native driver connector/PKI closure unavailable or changed") from exc


class SystemdSparkExecution(DockerSparkExecution):
    def __init__(self, binding, host=None):
        if (binding.get("execution_backend") != "native-systemd-v1"
                or len(set(binding["masters"])) != 3 or len(set(binding["workers"])) != 3):
            raise UnsafeTransition("native backend and full Spark endpoint set required")
        self.binding = binding
        self.masters, self.workers = tuple(binding["masters"]), tuple(binding["workers"])
        self.ca_file, self.client_cert, self.client_key = (binding[k] for k in ("ca_file", "client_cert", "client_key"))
        self.host = host or NativeHost(binding)

    def units(self, token):
        valid_token(token)
        if (token["owner"], token["generation"]) != (self.binding["owner"], self.binding["generation"]):
            raise UnsafeTransition("native fence belongs to another host/generation")
        saved = Path(self.binding["registry"])
        if saved.is_symlink() or saved.stat().st_mode & 0o077:
            raise UnsafeTransition("protected native registry required")
        records = json.loads(saved.read_text())
        if (records.get("owner"), records.get("generation"), records.get("epoch")) != (token["owner"], token["generation"], token["epoch"]):
            raise UnsafeTransition("native registry epoch mismatch; reconcile before takeover")
        units = records.get("units")
        if not isinstance(units, dict) or not units:
            raise UnsafeTransition("native complete group registry missing")
        expected = {}
        for query, unit in units.items():
            digest = identity(token, query)
            if unit != "mlo-driver-" + digest[:32] + ".service":
                raise UnsafeTransition("native unit name/identity mismatch")
            output = self.host.command(["systemctl", "show", unit, "--property=LoadState,Description,ActiveState,MainPID,ControlGroup,Restart,KillMode", "--no-pager"]).decode()
            props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            if (props.get("LoadState") != "loaded" or props.get("Description") != "MLO native driver " + digest
                    or props.get("Restart") != "no" or props.get("KillMode") != "control-group"):
                raise UnsafeTransition("native unit identity/restart policy mismatch")
            expected[unit] = props
        # Detect a partial group registry or a process outside the accepted set.
        observed = self.host.command(["systemctl", "list-units", "--all", "--plain", "--no-legend", "mlo-driver-*.service"]).decode()
        for line in observed.splitlines():
            name = line.split()[0] if line.split() else ""
            if name and name not in expected:
                raise UnsafeTransition("unregistered native driver unit; takeover blocked")
        return expected

    def stop_group(self, token):
        uncertainty = None
        try:
            units = self.units(token)
        except (UnsafeTransition, ValueError, OSError) as exc:
            # A partial registry/unit observation must block takeover, but must
            # not leave the other known local writers running during self-fence.
            uncertainty = exc
            valid_token(token)
            if (token["owner"], token["generation"]) != (self.binding["owner"], self.binding["generation"]):
                raise UnsafeTransition("native local fence identity mismatch") from exc
            path = Path(self.binding["registry"])
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise UnsafeTransition("native registry protection failed") from exc
            saved = json.loads(path.read_text())
            if (saved.get("owner"), saved.get("generation"), saved.get("epoch")) != (token["owner"], token["generation"], token["epoch"]):
                raise UnsafeTransition("native registry belongs to another epoch") from exc
            units = saved["units"]
            if not units or any(unit != "mlo-driver-" + identity(token, query)[:32] + ".service" for query, unit in units.items()):
                raise UnsafeTransition("native registry unit identity invalid") from exc
            units = {name: {} for name in units.values()}
        errors = []
        for name in units:
            try:
                self.host.command(["systemctl", "stop", name])
            except Exception as exc:
                errors.append(exc)
        if uncertainty is not None or errors:
            raise UnsafeTransition("known native writers stopped where possible; uncertain group still blocks takeover") from (uncertainty or errors[0])
        self.host.stopped(units)

    def observe(self, token):
        units = self.units(token)
        stopped = all(p.get("ActiveState") == "inactive" and p.get("MainPID") == "0" for p in units.values())
        if stopped:
            self.host.stopped(units)  # includes every nested cgroup and populated=0
        return self.cluster_proof(token, stopped)

    def receipt(self, token, operation):
        if operation == "stop":
            self.stop_group(token)
        elif operation != "observe":
            raise UnsafeTransition("native start requires the lease-bound admission API")
        proof = self.observe(token)
        units = self.units(token)
        registry = json.loads(Path(self.binding["registry"]).read_text())
        continuous = self.binding.get("continuous_drivers", [])
        required = {item["query"] for item in continuous}
        if required != {"weather-stream", "feedback-postgresql"}:
            raise UnsafeTransition("complete continuous native driver set required")
        running = all(registry["units"].get(query) in units
                      and units[registry["units"][query]].get("ActiveState") == "active"
                      and units[registry["units"][query]].get("MainPID", "0") != "0" for query in required)
        return {"owner": token["owner"], "generation": token["generation"], "epoch": token["epoch"],
                "operation": operation, "accepted": True, "execution_backend": "native-systemd-v1",
                "masters": list(self.masters), "workers": list(self.workers),
                "running": running, "native_units": sorted(units),
                "evidence": proof.__dict__}


def driver_unit(token, query, command, *, credentials, working_directory):
    """Reviewable transient unit properties. Never automatically re-admit a job.

    A trusted admission caller must CAS-check its live PostgreSQL lease and the
    maintenance barrier immediately before systemd-run and register the whole
    group durably before start. No ready flag enables that integration here.
    """
    digest = identity(token, query)
    if not command or command[0] != "/opt/mlo/native/spark-driver/bin/spark-submit":
        raise UnsafeTransition("accepted native driver executable required")
    if any(not isinstance(a, str) or "\x00" in a or "\n" in a for a in command):
        raise UnsafeTransition("native driver argv invalid")
    if not re.fullmatch(r"/srv/mlo/[a-z0-9._-]+/spark-driver", working_directory):
        raise UnsafeTransition("native driver persistent directory")
    argv = ["systemd-run", "--unit=mlo-driver-" + digest[:32],
            "--description=MLO native driver " + digest, "--service-type=exec",
            "--property=User=mlo", "--property=Group=mlo", "--property=Restart=no",
            "--property=KillMode=control-group", "--property=TimeoutStopSec=60s",
            "--property=RequiresMountsFor=/srv/mlo", "--property=MemoryMax=2G",
            "--property=CPUQuota=100%", "--property=NoNewPrivileges=yes",
            "--property=UMask=0077", "--property=WorkingDirectory=" + working_directory]
    for name in credentials:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise UnsafeTransition("native driver credential name")
        argv.append("--property=LoadCredential=" + name + ":/run/mlo/secrets/" + name)
    return [*argv, "--", *command]


class NativeAdmission:
    """Admit an approved application into a static, epoch-bound native unit.

    Static unit files deliberately remain loaded after stop so identity and
    cgroup evidence can be inspected. Old unit files are never auto-deleted.
    All durable registry changes precede process start; ambiguity blocks replay.
    """
    def __init__(self, binding, lease, barrier, host=None):
        self.binding, self.lease, self.barrier = binding, lease, barrier
        self.host = host or NativeHost(binding)

    def check(self, token):
        self.barrier.check()
        current = self.lease.read()
        if current != token or token["expires_at"] - self.lease.now() < 5:
            raise UnsafeTransition("native admission requires the current live lease with margin")
        if (token["owner"], token["generation"]) != (self.binding["owner"], self.binding["generation"]):
            raise UnsafeTransition("native admission owner/generation mismatch")
        self.host.mount()

    def admit(self, token, query, specification, specification_sha256):
        digest = identity(token, query)
        registry = Path(self.binding["registry"])
        with local_lock(str(registry) + ".lock"):
            self.check(token)
            path = Path(specification)
            if (not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o022
                    or hashlib.sha256(path.read_bytes()).hexdigest() != specification_sha256
                    or specification_sha256 not in self.binding.get("accepted_driver_specs", [])):
                raise UnsafeTransition("native driver specification not accepted")
            spec = json.loads(path.read_text())
            if spec.get("query") != query or spec.get("generation") != token["generation"]:
                raise UnsafeTransition("native driver query/generation mismatch")
            for field in ("app", "launcher", "spark_defaults", "python_executable"):
                source = self.host.path(spec[field]) if self.binding.get("lab") is True else Path(spec[field])
                if (not source.is_absolute() or source.is_symlink()
                        or hashlib.sha256(source.read_bytes()).hexdigest() != spec[field + "_sha256"]):
                    raise UnsafeTransition("native driver source/config checksum mismatch")
            if not str(Path(spec["app"])).startswith("/opt/mlo/current/apps/") or ".." in Path(spec["app"]).parts:
                raise UnsafeTransition("native driver app outside accepted release")
            if (not re.fullmatch(r"/opt/mlo/native/releases/spark-[a-f0-9]{16}", spec["spark_home"])
                    or not re.fullmatch(r"/opt/mlo/native/releases/java21-[a-f0-9]{16}", spec["java_home"])
                    or spec["command"][0] != spec["spark_home"] + "/bin/spark-submit"
                    or spec["app"] not in spec["command"]):
                raise UnsafeTransition("native driver command/release mismatch")
            verify_driver_connectors(spec, self.binding, self.host)
            state = json.loads(registry.read_text()) if registry.exists() else {"owner": token["owner"], "generation": token["generation"], "epoch": token["epoch"], "units": {}}
            if ((state["owner"], state["generation"], state["epoch"]) != (token["owner"], token["generation"], token["epoch"])
                    or state.get("pending")):
                raise UnsafeTransition("native driver registry requires reconciliation")
            unit = "mlo-driver-" + digest[:32] + ".service"
            if query in state["units"]:
                if state["units"][query] != unit:
                    raise UnsafeTransition("native duplicate admission identity")
                self.host.command(["systemctl", "is-active", "--quiet", unit])
                return {"unit": unit, "started": False}
            credentials = spec["credentials"]
            if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", c) for c in credentials):
                raise UnsafeTransition("native driver credential name")
            def quote(arg):
                if not isinstance(arg, str) or any(c in arg for c in "\n\r\x00"):
                    raise UnsafeTransition("native unit argument")
                return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
            if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))
                    or not re.fullmatch(r"/[A-Za-z0-9_./-]+", spec["spark_defaults"])):
                raise UnsafeTransition("native driver credential source paths invalid")
            body = ("[Unit]\nDescription=MLO native driver " + digest + "\nAfter=network-online.target\nRequiresMountsFor=/srv/mlo\n"
                    "[Service]\nType=exec\nUser=mlo\nGroup=mlo\nRestart=no\nKillMode=control-group\nTimeoutStopSec=60s\n"
                    "UMask=0077\nNoNewPrivileges=yes\nCPUQuota=100%\nMemoryMax=2G\n"
                    "RuntimeDirectory=mlo-driver-" + digest[:32] + "\nRuntimeDirectoryMode=0700\n"
                    + "".join("LoadCredential=" + c + ":/run/mlo/secrets/" + c + "\n" for c in credentials)
                    + "LoadCredential=driver-spec:" + str(path) + "\n"
                    + "LoadCredential=driver-defaults:" + spec["spark_defaults"] + "\n"
                    + "ExecStart=:" + " ".join(quote(x) for x in ("/usr/bin/python3", spec["launcher"]))
                    + ' "%d/driver-spec" ' + quote(specification_sha256) + "\n")
            target = self.host.path("/etc/systemd/system/" + unit)
            if target.exists() or target.is_symlink():
                raise UnsafeTransition("native unmanaged driver unit collision")
            state["units"][query] = unit
            state["pending"] = {"query": query, "specification_sha256": specification_sha256}
            atomic_json(registry, state)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x") as stream:
                    stream.write(body)
                target.chmod(0o600)
                self.host.command(["systemd-analyze", "verify", str(target)])
                self.host.command(["systemctl", "daemon-reload"])
                self.check(token)
                self.host.command(["systemctl", "start", unit])
                self.check(token)
                self.host.command(["systemctl", "is-active", "--quiet", unit])
                state["pending"] = None
                atomic_json(registry, state)
                return {"unit": unit, "started": True}
            except BaseException:
                self.host.command(["systemctl", "stop", unit])
                self.host.stopped([unit])
                raise UnsafeTransition("native admission failed/fenced; reconcile pending registry")
