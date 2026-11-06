"""Host-bound runtime installer. Bootstrap execution uses only Python stdlib.

Protected host-binding v1 supplies target, mount UUID, accepted manifest digests,
images and secret filenames. `lab:true` permits only an explicit disposable root.
Production always uses / and a root-owned /etc/mlo/host-binding.json. No command or
script supplied in a manifest is executed. Compose and systemctl argv are fixed.
"""

from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid


class InstallError(ValueError):
    pass


def require(value, message):
    if not value:
        raise InstallError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / (".partial-" + uuid.uuid4().hex)
    try:
        with temporary.open("x") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def deployment_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("concurrent deploy refused") from exc
        yield
    finally:
        os.close(fd)


def protected_binding(path):
    path = Path(path).absolute()
    require(path.is_file() and not path.is_symlink(), "protected binding file required")
    mode = path.stat().st_mode
    require(
        stat.S_IMODE(mode) & 0o077 == 0, "binding must not be group/world accessible"
    )
    binding = read(path)
    require(binding.get("schema_version") == "1.0.0", "host binding schema mismatch")
    require(isinstance(binding.get("lab", False), bool), "invalid lab binding")
    root = Path(binding.get("root", "/"))
    require(
        root.is_absolute() and root.resolve() == root, "canonical host root required"
    )
    if binding.get("lab") is True:
        require(
            root != Path("/") and root in path.parents,
            "lab binding must be inside disposable root",
        )
        require(
            root.name.startswith("mlo-") or "runtime-lab" in root.name,
            "explicit disposable runtime lab root required",
        )
    else:
        require(
            path == Path("/etc/mlo/host-binding.json") and root == Path("/"),
            "production binding location mismatch",
        )
        require(
            path.stat().st_uid == 0 and os.geteuid() == 0,
            "production root ownership required",
        )
        require(
            platform.system() == "Linux" and platform.machine() in {"x86_64", "amd64"},
            "production linux/amd64 required",
        )
    for ancestor in path.parents:
        require(not ancestor.is_symlink(), "binding ancestor symlink refused")
    require(
        re.fullmatch(r"i-[a-f0-9]{8,17}", binding.get("instance_id", "")),
        "instance identity required",
    )
    for field in ["node", "environment", "generation"]:
        require(
            isinstance(binding.get(field), str)
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", binding[field]),
            "invalid protected target identity",
        )
    require(
        binding.get("role")
        in {
            "control",
            "gateway",
            "postgresql",
            "mongodb",
            "kafka",
            "spark",
            "elasticsearch",
            "advertising",
        },
        "invalid protected node role",
    )
    return binding


def host_path(binding, path):
    root = Path(binding.get("root", "/"))
    result = root / path.lstrip("/")
    require(".." not in result.parts, "parent host path refused")
    for ancestor in [result, *result.parents]:
        if ancestor == root.parent:
            break
        require(not ancestor.is_symlink(), "host path symlink refused")
    return result


def validate_identity(role, node, data, identity):
    """Never format Kafka storage; initialize ZooKeeper myid only when truly empty."""
    data = Path(data)
    if role not in {"kafka", "spark"}:
        return
    expected = re.fullmatch(
        ("kafka" if role == "kafka" else "spark") + r"-0([123])", node
    )
    require(
        expected and identity.get("node_id") == int(expected.group(1)),
        "accepted node identity mismatch",
    )
    sid = int(expected.group(1))
    if role == "kafka":
        require(
            re.fullmatch(r"[A-Za-z0-9_-]{22}", identity.get("cluster_id", "")),
            "accepted KRaft cluster ID required",
        )
        metadata = data / "kafka/meta.properties"
        require(
            metadata.is_file() and not metadata.is_symlink(),
            "Kafka storage must be initialized separately and inspected",
        )
        values = {}
        for line in metadata.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                require(key.strip() not in values, "duplicate Kafka metadata property")
                values[key.strip()] = value.strip()
        require(
            values.get("cluster.id") == identity["cluster_id"]
            and values.get("node.id") == str(sid),
            "existing Kafka cluster/node identity mismatch",
        )
    else:
        directory = data / "zookeeper"
        directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        myid = directory / "myid"
        if myid.exists():
            require(
                myid.is_file()
                and not myid.is_symlink()
                and myid.read_text().strip() == str(sid),
                "existing ZooKeeper myid mismatch",
            )
        else:
            require(
                not any(directory.iterdir()),
                "nonempty ZooKeeper storage without myid refused",
            )
            fd = os.open(
                myid, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
            )
            with os.fdopen(fd, "w") as stream:
                stream.write(str(sid) + "\n")
                stream.flush()
                os.fsync(stream.fileno())


class ComposeSystemdBackend:
    """Fixed local host commands; no shell, manifest hooks or remote commands."""

    def __init__(self, binding):
        self.binding = binding

    def command(self, argv):
        if getattr(self, "operation_guard", None):
            self.operation_guard.check()
        try:
            result = subprocess.run(
                argv,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
            ).stdout
            if getattr(self, "operation_guard", None):
                self.operation_guard.check()
            return result
        except (subprocess.SubprocessError, OSError) as exc:
            raise InstallError("controlled host command failed") from exc

    def check_mount(self, path):
        if self.binding.get("lab") is True:
            # Docker lab tests use a dedicated mounted named volume and this exact
            # target. Host-process unit tests inject a backend instead.
            require(
                self.binding.get("mount_target") == str(path),
                "lab mount target acceptance missing",
            )
            target = (
                self.command(["findmnt", "-rn", "-T", str(path), "-o", "TARGET"])
                .decode()
                .strip()
            )
            require(target == str(path), "lab persistent root is not a dedicated mount")
        else:
            require(self.binding.get("mount_uuid"), "accepted mount UUID required")
            target = (
                self.command(["findmnt", "-rn", "-T", str(path), "-o", "TARGET"])
                .decode()
                .strip()
            )
            actual = (
                self.command(["findmnt", "-rn", "-T", str(path), "-o", "UUID"])
                .decode()
                .strip()
            )
            require(
                target == str(path) and actual == self.binding["mount_uuid"],
                "persistent mount identity mismatch",
            )

    def compose(self, release, *args):
        path = release / "installed-compose.json"
        prefix = (
            "mlo-p4-b-install-" if self.binding.get("lab") is True else "mlo-runtime-"
        )
        return self.command(
            [
                "docker",
                "compose",
                "--project-name",
                prefix + self.binding["node"],
                "--file",
                str(path),
                *args,
            ]
        )

    def start(self, release):
        services = read(release / "installed-compose.json")["services"]
        if services:
            if not self.binding.get("lab"):
                require(
                    self.command(["docker", "compose", "version", "--short"])
                    .decode()
                    .strip()
                    == "5.5.1",
                    "accepted Compose 5.5.1 required",
                )
            self.compose(release, "config", "--quiet")
            self.compose(
                release,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                "--remove-orphans",
            )

    def stop(self, release):
        if read(release / "installed-compose.json")["services"]:
            self.compose(release, "down", "--remove-orphans")  # Never --volumes.

    def health(self, release):
        services = read(release / "installed-compose.json")["services"]
        if not services:
            require(
                self.binding.get("lab") is True, "empty operational runtime refused"
            )
            return
        state = (
            self.compose(release, "ps", "--all", "--format", "json").decode().strip()
        )
        try:
            records = json.loads(state)
            if isinstance(records, dict):
                records = [records]
        except json.JSONDecodeError:
            records = [json.loads(line) for line in state.splitlines() if line.strip()]
        by_service = {r["Service"]: r for r in records}
        require(
            set(by_service) == set(services), "missing or unexpected Compose services"
        )
        require(
            all(
                r.get("State") == "running" and r.get("Health") == "healthy"
                for r in by_service.values()
            ),
            "runtime readiness failed",
        )

    def units(self, release):
        if self.binding.get("lab") is True and not self.binding.get(
            "systemd_available", False
        ):
            require(
                not list((release / "units").glob("*.service"))
                and not list((release / "units").glob("*.timer")),
                "lab lacks systemd; real unit activation remains unverified",
            )
            return
        # The bootstrap verifier/installer needs stdlib only. Host timers/supervisor
        # need their separately provisioned, pinned offline Python environment.
        if any((release / "units").iterdir()):
            required = {"jsonschema": "4.25.1", "PyYAML": "6.0.3"}
            if self.binding["role"] == "control":
                required["psycopg"] = "3.3.6"
            for distribution, expected in required.items():
                try:
                    actual = importlib.metadata.version(distribution)
                except importlib.metadata.PackageNotFoundError as exc:
                    raise InstallError(
                        "accepted offline host Python runtime dependency missing"
                    ) from exc
                require(
                    actual == expected,
                    "host Python runtime dependency version mismatch",
                )
        target = Path("/etc/systemd/system")
        expected = {p.name for p in (release / "units").iterdir()}
        # Disable stale units from the previous managed release; never other units.
        for path in target.glob("mlo-*"):
            if (
                path.suffix in {".service", ".timer"}
                and path.is_symlink()
                and "/opt/mlo/releases/" in str(path.readlink())
                and path.name not in expected
            ):
                self.command(["systemctl", "disable", "--now", path.name])
                path.unlink()
        for path in (release / "units").iterdir():
            destination = target / path.name
            require(
                not destination.exists()
                or destination.is_symlink()
                and "/opt/mlo/releases/" in str(destination.readlink()),
                "unmanaged systemd unit exists",
            )
            temporary = target / ("." + path.name + "-" + uuid.uuid4().hex)
            temporary.symlink_to(path)
            os.replace(temporary, destination)
        self.command(["systemctl", "daemon-reload"])
        for path in (release / "units").iterdir():
            if (
                path.suffix == ".timer"
                or path.name == "mlo-backup-before-shutdown.service"
                or path.name == "mlo-supervisor.service"
            ):
                self.command(["systemctl", "enable", "--now", path.name])
                self.command(["systemctl", "is-active", "--quiet", path.name])
        self.unit_health(release)

    def unit_health(self, release):
        if self.binding.get("lab") is True:
            return
        for path in (release / "units").iterdir():
            if path.suffix == ".timer" or path.name in {
                "mlo-backup-before-shutdown.service",
                "mlo-supervisor.service",
            }:
                self.command(["systemctl", "is-active", "--quiet", path.name])
        if self.binding["role"] == "control":
            argv = [
                "/usr/bin/env",
                "PYTHONPATH=/opt/mlo/current/aws-infra/runtime/src",
                "/usr/bin/python3",
                "-m",
                "mlo_runtime.supervisor",
                "--binding",
                "/etc/mlo/supervisor.json",
                "--check",
            ]
            for attempt in range(10):
                try:
                    self.command(argv)
                    break
                except InstallError:
                    if attempt == 9:
                        raise
                    time.sleep(1)


def verify_payload(payload, binding, operation):
    manifest = read(payload / "manifest.json")
    require(
        sha(payload / "manifest.json") in binding.get("accepted_manifests", []),
        "manifest not accepted by protected host binding",
    )
    require(
        manifest.get("schema_version") == "1.0.0"
        and manifest.get("example_only") is False
        and manifest.get("platform") == "linux/amd64",
        "invalid execution artifact",
    )
    require(
        manifest.get("environment") == binding["environment"]
        and manifest.get("generation") == binding["generation"],
        "artifact target generation mismatch",
    )
    require(
        binding["node"] in manifest.get("target_nodes", [])
        and manifest.get("node_roles", {}).get(binding["node"]) == binding["role"],
        "artifact node/role mismatch",
    )
    require(operation in manifest.get("operations", []), "operation not permitted")
    require(
        not manifest.get("fixture", False) or binding.get("lab") is True,
        "fixture artifact cannot execute on production",
    )
    for name, expected in manifest["files"].items():
        require(
            not name.startswith("/")
            and ".." not in Path(name).parts
            and "\\" not in name,
            "unsafe payload file",
        )
        path = payload / name
        require(
            path.is_file() and not path.is_symlink() and sha(path) == expected,
            "payload file verification failed",
        )
    actual = {str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file()}
    require(
        actual == set(manifest["files"]) | {"manifest.json"}, "unexpected payload files"
    )
    require(
        set(manifest["image_digests"].values()).issubset(
            set(binding.get("image_digests", {}).values())
        ),
        "images not accepted by host binding",
    )
    required = manifest.get("required_secrets", {}).get(binding["node"])
    require(
        isinstance(required, list)
        and set(required) == set(binding.get("required_secrets", [])),
        "secret acceptance mismatch",
    )
    for name in required:
        require(
            re.fullmatch(r"/run/mlo/secrets/[A-Za-z0-9_.-]+", name),
            "invalid secret filename",
        )
        secret = host_path(binding, name)
        require(
            secret.is_file()
            and secret.stat().st_size > 0
            and stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0,
            "required protected secret missing",
        )
    return manifest


def prepare_persistence(binding, data):
    """Create new service directories with explicitly accepted image UID/GID only."""
    names = {
        "spark": ["spark", "zookeeper"],
        "kafka": ["kafka"],
        "elasticsearch": ["elasticsearch"],
        "postgresql": ["postgresql"],
        "mongodb": ["mongodb"],
        "control": ["spool"],
        "gateway": ["spool"],
        "advertising": ["spool"],
    }[binding["role"]]
    for name in names:
        owner = binding.get("persistent_owners", {}).get(name)
        if binding.get("lab") is True and owner is None:
            continue
        require(
            isinstance(owner, dict)
            and all(
                isinstance(owner.get(k), int)
                and not isinstance(owner[k], bool)
                and 0 <= owner[k] <= 65535
                for k in ["uid", "gid"]
            ),
            "accepted persistent image UID/GID required",
        )
        target = data / name
        require(not target.is_symlink(), "persistent directory symlink refused")
        if not target.exists():
            target.mkdir(parents=True, mode=0o750)
            os.chown(target, owner["uid"], owner["gid"])
        require(
            target.is_dir()
            and (target.stat().st_uid, target.stat().st_gid)
            == (owner["uid"], owner["gid"]),
            "existing persistent ownership mismatch; no recursive chown is automatic",
        )


def require_stateful_change(binding, generation):
    if binding["role"] in {"kafka", "spark", "elasticsearch", "postgresql", "mongodb"}:
        backup = binding.get("stateful_change", {})
        require(
            backup.get("backup_verified") is True
            and backup.get("remote_copy_verified") is True
            and backup.get("generation") == generation
            and backup.get("expires_at", 0) > time.time(),
            "stateful replacement requires current verified backup and remote copy acceptance",
        )


def prepare_release(payload, release, binding, manifest):
    require(
        re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", manifest.get("artifact_version", "")),
        "invalid artifact version",
    )
    if release.exists():
        require(
            read(release / "release.json")["manifest_sha256"]
            == sha(payload / "manifest.json"),
            "immutable release version reused",
        )
        return
    stage = release.parent / (".staging-" + uuid.uuid4().hex)
    try:
        shutil.copytree(payload, stage, symlinks=False)
        host = stage / "hosts" / binding["node"]
        accepted = read(host / "compose.json")
        require(
            accepted.get("services") or manifest.get("fixture") and binding.get("lab"),
            "empty runtime only allowed for explicit fixture",
        )
        config = host / "config"
        config.mkdir()
        for source in host.iterdir():
            if (
                source.is_file()
                and source.name not in {"compose.yml", "compose.json"}
                and source.suffix not in {".service", ".timer"}
            ):
                shutil.copy2(source, config / source.name)
        final_host = release / "hosts" / binding["node"]
        for service in accepted.get("services", {}).values():
            require(
                re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", service.get("image", "")),
                "mutable or unresolved Compose image",
            )
            require(
                service["image"] in manifest["image_digests"].values(),
                "unbound Compose image",
            )
            # Manifest may select command/entrypoint as application configuration, but
            # the installer never executes it directly or accepts a shell deploy hook.
            volumes = []
            for volume in service.get("volumes", []):
                require(isinstance(volume, str), "explicit host volume required")
                source, target, *mode = volume.split(":")
                if source.startswith("/etc/mlo/current/config/"):
                    source = str(final_host / "config" / Path(source).name)
                elif source.startswith("/srv/mlo/") or source == "/run/mlo/secrets":
                    source = str(host_path(binding, source))
                elif source == "/opt/mlo/current":
                    source = str(release)
                elif source == "/etc/mlo/relay.json":
                    source = str(host_path(binding, source))
                    require(
                        Path(source).is_file()
                        and stat.S_IMODE(Path(source).stat().st_mode) & 0o077 == 0,
                        "accepted relay binding missing",
                    )
                else:
                    raise InstallError("unaccepted host mount")
                volumes.append(":".join([source, target, *mode]))
            service["volumes"] = volumes
        atomic_json(stage / "installed-compose.json", accepted)
        units = stage / "units"
        units.mkdir()
        for path in host.iterdir():
            if path.suffix in {".service", ".timer"}:
                require(
                    re.fullmatch(r"mlo-[a-z0-9-]+\.(service|timer)", path.name),
                    "unaccepted systemd unit name",
                )
                content = path.read_text().replace(
                    "/opt/mlo/current/src", "/opt/mlo/current/aws-infra/runtime/src"
                )
                (units / path.name).write_text(content)
                (units / path.name).chmod(0o644)
        atomic_json(
            stage / "release.json",
            {
                "manifest_sha256": sha(payload / "manifest.json"),
                "artifact_version": manifest["artifact_version"],
            },
        )
        os.replace(stage, release)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def switch(path, target):
    temporary = path.parent / ("." + path.name + "-" + uuid.uuid4().hex)
    temporary.symlink_to(target)
    os.replace(temporary, path)


def execute(
    payload,
    binding_path,
    operation,
    environment,
    generation,
    node,
    *,
    backend=None,
    operation_guard=None,
):
    binding = protected_binding(binding_path)
    root = Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root / "aws-infra/contracts"))
    if binding.get("lab") is True or operation_guard is not None:
        return _execute(
            payload,
            binding_path,
            operation,
            environment,
            generation,
            node,
            backend=backend,
            operation_guard=operation_guard,
        )
    require(
        binding.get("coordination_contract") and binding.get("coordination_scope"),
        "common deployment contract missing",
    )
    from infra_common.factory import load_guard

    guard = load_guard(
        binding["coordination_contract"],
        binding["coordination_scope"],
        "runtime-" + node,
        "deploy",
    )
    with guard.hold():
        return _execute(
            payload,
            binding_path,
            operation,
            environment,
            generation,
            node,
            backend=backend,
            operation_guard=guard,
        )


def _execute(
    payload,
    binding_path,
    operation,
    environment,
    generation,
    node,
    *,
    backend=None,
    operation_guard=None,
):
    payload = Path(payload).resolve()
    binding = protected_binding(binding_path)
    require(
        (environment, generation, node)
        == (binding["environment"], binding["generation"], binding["node"]),
        "execution target differs from protected host binding",
    )
    require(
        operation in {"install", "deploy", "health", "rollback"}, "invalid operation"
    )
    if operation_guard is not None:
        from infra_common.coordination import require_operation

        token = require_operation(operation_guard, "deploy")
        require(
            token["scope"]["environment"] == environment
            and token["scope"]["generation"] == generation,
            "common coordination target mismatch",
        )
    require(
        binding.get("lab") is True or operation_guard is not None,
        "accepted common deployment guard required",
    )
    install_root = host_path(binding, "/opt/mlo")
    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backend = backend or ComposeSystemdBackend(binding)
    if isinstance(backend, ComposeSystemdBackend):
        backend.operation_guard = operation_guard
    receipt = {
        "operation": operation,
        "node": node,
        "environment": environment,
        "generation": generation,
        "status": "failed",
        "changed": False,
        "lab": binding.get("lab", False),
        "ready_for_aws": False,
    }
    with deployment_lock(install_root / "deploy.lock"):
        receipts = install_root / "receipts"
        try:
            manifest = verify_payload(payload, binding, operation)
            receipt["artifact_version"] = manifest["artifact_version"]
            data = host_path(binding, "/srv/mlo")
            backend.check_mount(data)
            prepare_persistence(binding, data / generation)
            validate_identity(
                binding["role"],
                node,
                data / generation,
                binding.get("cluster_identity", {}),
            )
            if binding["role"] == "spark" and "zookeeper" in binding.get(
                "persistent_owners", {}
            ):
                owner = binding["persistent_owners"]["zookeeper"]
                os.chown(
                    data / generation / "zookeeper/myid", owner["uid"], owner["gid"]
                )
            state_path = install_root / "state.json"
            state = (
                read(state_path)
                if state_path.exists()
                else {"current": None, "previous": None}
            )
            release_root = install_root / "releases"
            release_root.mkdir(exist_ok=True, mode=0o700)
            release = release_root / manifest["artifact_version"]
            old = release_root / state["current"] if state["current"] else None
            if operation == "health":
                require(
                    old is not None and old.exists(), "no active installed revision"
                )
                require(
                    old == release,
                    "health artifact version differs from active release",
                )
                backend.health(old)
                if hasattr(backend, "unit_health"):
                    backend.unit_health(old)
            elif operation == "rollback":
                require(state["previous"], "no previous accepted revision")
                release = release_root / state["previous"]
                require(release.is_dir(), "previous release missing")
                require_stateful_change(binding, generation)
                deploy(release, old, backend, install_root, binding, state_path)
                receipt["changed"] = True
            else:
                prepare_release(payload, release, binding, manifest)
                if old == release:
                    backend.health(release)
                    if hasattr(backend, "unit_health"):
                        backend.unit_health(release)
                else:
                    if old:
                        require_stateful_change(binding, generation)
                    deploy(release, old, backend, install_root, binding, state_path)
                    receipt["changed"] = True
            receipt["status"] = "success"
            return receipt
        except BaseException as exc:
            receipt["error_type"] = type(exc).__name__
            raise
        finally:
            atomic_json(
                receipts / (str(time.time_ns()) + "-" + operation + ".json"), receipt
            )


def deploy(release, old, backend, install_root, binding, state_path):
    current = install_root / "current"
    config_root = host_path(binding, "/etc/mlo")
    config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_current = config_root / "current"
    try:
        backend.start(release)
        backend.health(release)
        switch(current, release)
        switch(config_current, release / "hosts" / binding["node"])
        backend.units(release)
        atomic_json(
            state_path, {"current": release.name, "previous": old.name if old else None}
        )
    except BaseException as original:
        if old:
            try:
                backend.start(old)
                backend.health(old)
                switch(current, old)
                switch(config_current, old / "hosts" / binding["node"])
                backend.units(old)
            except BaseException as rollback:
                raise InstallError(
                    "deployment and rollback failed; manual recovery required"
                ) from rollback
        else:
            if hasattr(backend, "stop"):
                backend.stop(release)
            if current.is_symlink():
                current.unlink()
            if config_current.is_symlink():
                config_current.unlink()
        raise original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--operation",
        choices=["install", "deploy", "health", "rollback"],
        required=True,
    )
    for item in ["environment", "generation", "node"]:
        parser.add_argument("--" + item, required=True)
    parser.add_argument("--binding", default="/etc/mlo/host-binding.json")
    args = parser.parse_args()
    payload = Path(__file__).resolve().parents[4]
    try:
        result = execute(
            payload,
            args.binding,
            args.operation,
            args.environment,
            args.generation,
            args.node,
        )
    except (ValueError, OSError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "ready_for_aws": False,
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
