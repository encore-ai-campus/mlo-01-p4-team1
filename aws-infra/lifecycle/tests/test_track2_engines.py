"""C-owned Docker runs: actual PG/Mongo dump/copy/independent restore/source stop.

Version storage and service observations are explicit local fixtures. No SDK,
real S3, app migration, existing containers or other-role artifact writes.
"""

import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import time
import unittest
import uuid

from mlo_lifecycle.journal import atomic, locked, sign
from mlo_lifecycle.model import BASE, ROOT, digest, read
from mlo_lifecycle.runtime import stop_engine_backends
from mlo_lifecycle.track2 import initialize, sealed
from mlo_runtime.database_stop import ContainerStatefulBackend
from infra_common.evidence import SERVICES
from track2_support import track2_fixture


class LocalVersions:
    """Immutable file versions in this test run, not actual S3 durability."""
    def __init__(self, folder):
        self.folder = folder
        folder.mkdir()

    def put(self, name, raw):
        version = hashlib.sha256(raw).hexdigest()
        path = self.folder / (digest(name) + "-" + version + ".dump")
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return {"location": str(path), "version_id": version, "sha256": version}

    def get(self, receipt):
        path = Path(receipt["location"])
        if path.parent != self.folder or path.is_symlink():
            raise ValueError("unbound fixture copy")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("changed fixture version")
        return raw


class Track2EngineIntegration(unittest.TestCase):
    def setUp(self):
        self.run_id = "mlo-c-track2-" + uuid.uuid4().hex[:12]
        self.folder = BASE / ".artifacts" / self.run_id
        self.folder.mkdir(parents=True, mode=0o700)
        self.containers, self.volumes = [], []
        self.platform = {"arm64": "linux/arm64", "aarch64": "linux/arm64", "x86_64": "linux/amd64"}[platform.machine()]
        self.images = read(ROOT / "aws-infra/runtime/images.lock.json")["images"]
        self.bundle = track2_fixture()
        self.spec = self.bundle["track2"]
        initialize(self.folder, self.spec)
        state = read(self.folder / "track2-state.json")
        state["admission_closed"] = state["restarts_disabled"] = True
        for writer in state["writers"].values():
            writer["stopped"] = True
        atomic(self.folder / "track2-state.json", state)

    def command(self, argv, *, input=None):
        result = subprocess.run(argv, input=input, capture_output=True, timeout=180)
        with (self.folder / "commands.log").open("ab") as stream:
            stream.write((" ".join(argv) + "\n").encode() + result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError("local Docker command failed; see C run commands.log")
        return result.stdout

    def database(self, node, side):
        name = self.run_id + "-" + node + "-" + side
        image = self.images[node]["ref"] + "@" + self.images[node]["digest"]
        declared = json.loads(self.command(["docker", "image", "inspect", image, "--format", "{{json .Config.Volumes}} "])) or {}
        argv = ["docker", "create", "--name", name, "--label", "mlo.c.run=" + self.run_id,
                "--network", "none", "--platform", self.platform, "--memory", "512m", "--cpus", "1"]
        for number, target in enumerate(sorted(declared)):
            volume = name + "-" + str(number)
            self.command(["docker", "volume", "create", "--label", "mlo.c.run=" + self.run_id, volume])
            self.volumes.append(volume)
            argv.extend(["--mount", "type=volume,source=" + volume + ",target=" + target])
        if node == "postgresql":
            argv.extend(["--env", "POSTGRES_PASSWORD=synthetic-local-only", "--env", "POSTGRES_DB=mlo_lifecycle"])
        argv.append(image)
        if node == "mongodb":
            argv.extend(["mongod", "--bind_ip", "127.0.0.1"])
        identity = self.command(argv).decode().strip()
        self.containers.append(identity)
        self.command(["docker", "start", identity])
        for _ in range(90):
            # pg_isready can return success before POSTGRES_DB is initialized;
            # require an actual query against the exact test database instead.
            ready = (["psql", "-U", "postgres", "-d", "mlo_lifecycle", "-tAc", "SELECT 1"]
                     if node == "postgresql" else
                     ["mongosh", "--quiet", "--eval", "quit(db.adminCommand({ping:1}).ok===1?0:1)"])
            result = subprocess.run(["docker", "exec", identity, *ready], capture_output=True, timeout=10)
            if result.returncode == 0 and (node != "postgresql" or result.stdout.strip() == b"1"):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("new C database readiness timed out")
        if side == "source":
            if node == "postgresql":
                self.command(["docker", "exec", identity, "psql", "-U", "postgres", "-d", "mlo_lifecycle", "-v", "ON_ERROR_STOP=1", "-c",
                              "CREATE ROLE app_writer LOGIN; CREATE TABLE events(id serial primary key,payload text not null unique); INSERT INTO events(payload) VALUES('synthetic-a'),('synthetic-b'); GRANT INSERT,SELECT ON events TO app_writer;"])
            else:
                self.command(["docker", "exec", identity, "mongosh", "--quiet", "--eval",
                              'const t=db.getSiblingDB("mlo_lifecycle");t.log_record.insertMany([{id:"synthetic-a"},{id:"synthetic-b"}]);t.log_record.createIndex({id:1},{unique:true});'])
        return identity

    def tearDown(self):
        # Exact recorded IDs/names only. Assert the run label before removing each resource.
        errors = []
        for identity in reversed(self.containers):
            try:
                value = json.loads(self.command(["docker", "inspect", identity]))[0]
                self.assertEqual(value["Config"]["Labels"]["mlo.c.run"], self.run_id)
                self.command(["docker", "rm", "--force", identity])
            except Exception as exc:
                errors.append(type(exc).__name__)
        for volume in reversed(self.volumes):
            try:
                value = json.loads(self.command(["docker", "volume", "inspect", volume]))[0]
                self.assertEqual(value["Labels"]["mlo.c.run"], self.run_id)
                self.command(["docker", "volume", "rm", volume])
            except Exception as exc:
                errors.append(type(exc).__name__)
        remaining = {"run_id": self.run_id, "cleanup_errors": errors,
                     "containers": self.command(["docker", "ps", "-aq", "--filter", "label=mlo.c.run=" + self.run_id]).decode().split(),
                     "volumes": self.command(["docker", "volume", "ls", "-q", "--filter", "label=mlo.c.run=" + self.run_id]).decode().split()}
        atomic(self.folder / "resources.json", remaining)
        self.assertEqual(remaining["cleanup_errors"], [])
        self.assertEqual(remaining["containers"], [])
        self.assertEqual(remaining["volumes"], [])

    def test_real_dumps_version_copies_independent_restores_and_source_stop(self):
        inventory = self.bundle["inventory"]
        spec = self.spec

        class SyntheticGuard:
            operation = "destroy"
            def check(self):
                return {"operation_id": "synthetic-track2-stop", "epoch": spec["epoch"],
                        "scope": {"generation": inventory["generation"]}}

        guard = SyntheticGuard()
        backends = {}
        for node in ("postgresql", "mongodb"):
            source, target = self.database(node, "source"), self.database(node, "restore")
            binding = {"lab": True, "node": node, "generation": inventory["generation"],
                       "database": "mlo_lifecycle", "source_id": source, "restore_id": target,
                       "admin_user": "postgres", "writer_roles": ["app_writer"]}
            backends[node] = ContainerStatefulBackend(binding, self.folder / node,
                              LocalVersions(self.folder / (node + "-versions")), command=self.command,
                              operation_guard=guard)
        key = b"C-only-synthetic-observer-000000000000"

        def evidence():
            now = time.time()
            return sign({"schema_version": "runtime-shutdown-evidence-v1", "source": "local-lab",
                         "operation_id": guard.check()["operation_id"], "epoch": spec["epoch"],
                         "generation": inventory["generation"], "nodes": {n["name"]: {"instance_id": n["instance_id"],
                         "generation": inventory["generation"]} for n in inventory["nodes"]},
                         "services": {name: True for name in SERVICES}, "issuer": "C-fixture",
                         "observed_at": now, "expires_at": now + 30}, key)

        with locked(self.folder):
            result = stop_engine_backends(inventory, backends, guard, evidence, {"C-fixture": key},
                       self.folder / "receipts", track2_check=lambda: sealed(self.folder, spec))
        self.assertTrue(result["complete"])
        for node, backend in backends.items():
            receipt = read(self.folder / "receipts" / (node + ".receipt.json"))
            self.assertEqual(receipt["phase"], "complete")
            self.assertTrue(receipt["independent_copy"]["version_id"])
            self.assertTrue(receipt["restore_verification"]["verified"])
            self.assertTrue(backend.observe_stopped(node))
            self.assertEqual(backend.fingerprint(backend.binding["restore_id"]), read(backend.control)["fingerprint"])
        atomic(self.folder / "acceptance.json", {"actual_database_dump_restore_stop": "pass",
               "track2_hooks": "synthetic", "versions": "local-file-fixture", "source_unchanged_before_stop": True,
               "platform": self.platform, "ready_for_aws": False, "actual_app_hooks": "not_run"})
