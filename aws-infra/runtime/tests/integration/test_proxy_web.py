import json
import os
from pathlib import Path
import socket
import time
import unittest

from mlo_runtime.contracts import RUNTIME
from mlo_runtime.lab import Lab
from mlo_runtime.render import haproxy
from mlo_runtime.operations import UnsafeTransition, atomic_json, rollout
from mlo_runtime.web import DockerWebBackend


class ProxyWebIntegration(unittest.TestCase):
    def test_actual_haproxy_failover_to_backup_after_a_stop(self):
        lab = Lab("proxy")
        self.addCleanup(lab.close)
        cfg = haproxy(
            {
                "nodes": [
                    {"name": "a", "private_ip": "a", "role": "gateway"},
                    {"name": "b", "private_ip": "b", "role": "gateway"},
                ]
            }
        )
        cfg = cfg.replace("127.0.0.1:3129", ":3129").replace(
            "server b b:3128 check", "server b b:3128 backup check"
        )
        (lab.folder / "haproxy.cfg").write_text(cfg)
        lab.configure(
            {
                "image": lab.image("haproxy"),
                "volumes": [
                    f"{lab.folder / 'haproxy.cfg'}:/usr/local/etc/haproxy/haproxy.cfg:ro"
                ],
            }
        )
        config = json.loads(lab.compose_path.read_text())
        for name in ["a", "b"]:
            config["services"][name] = {
                "image": "mlo-p4-b-host-lab:20261029",
                "platform": os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64"),
                "labels": {"mlo.role": "B", "mlo.run": lab.project},
                "mem_limit": "128m",
                "command": ["python3", "-m", "http.server", "3128"],
                "working_dir": "/tmp",
                "healthcheck": {
                    "test": ["CMD", "curl", "--fail", "http://127.0.0.1:3128/"],
                    "interval": "1s",
                    "timeout": "1s",
                    "retries": 10,
                },
            }
        config["services"]["proxy"]["depends_on"] = {
            name: {"condition": "service_healthy"} for name in ["a", "b"]
        }
        lab.compose_path.write_text(json.dumps(config))
        lab.up()
        lab.compose("exec", "-T", "a", "sh", "-c", "printf a > /tmp/identity")
        lab.compose("exec", "-T", "b", "sh", "-c", "printf b > /tmp/identity")

        def query():
            return lab.compose(
                "exec",
                "-T",
                "b",
                "curl",
                "--fail",
                "--silent",
                "--max-time",
                "2",
                "http://proxy:3129/identity",
            ).stdout

        self.assertEqual(query(), b"a")
        lab.compose("stop", "a")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if query() == b"b":
                    break
            except RuntimeError:
                pass
            time.sleep(0.5)
        else:
            self.fail("HAProxy did not route new connections to B")

    def test_docker_web_replacement_and_rollback_preserves_old_container_config(self):
        lab = Lab("web")
        self.addCleanup(lab.close)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        image = json.loads(
            lab.docker(
                "image",
                "inspect",
                "mlo-p4-b-host-lab:20261029",
                "--format",
                "{{json .RepoDigests}}",
            ).stdout
        )[0]
        # A local image ID is an immutable digest; no registry publication occurs.
        revisions = {}
        for version in ["old", "new", "bad"]:
            file = lab.folder / (version + ".txt")
            file.write_text(version)
            revisions[version] = {
                "version": version,
                "migration_compatible": True,
                "compose": {
                    "services": {
                        "web": {
                            "image": image,
                            "pull_policy": "never",
                            "platform": os.environ.get(
                                "RUNTIME_LAB_PLATFORM", "linux/arm64"
                            ),
                            "labels": {"mlo.role": "B", "mlo.run": lab.project},
                            "mem_limit": "256m",
                            "volumes": [
                                f"{file}:/revision:ro",
                                f"{RUNTIME / 'tests/integration/smoke_http.py'}:/smoke.py:ro",
                            ],
                            "ports": [f"127.0.0.1:{port}:8000"],
                            "command": [
                                "python3",
                                "/smoke.py",
                                "--port",
                                "8000",
                                "--revision",
                                "/revision",
                            ],
                            "healthcheck": {
                                "test": [
                                    "CMD",
                                    "curl",
                                    "--fail",
                                    "http://127.0.0.1:8000/ready",
                                ],
                                "interval": "1s",
                                "timeout": "1s",
                                "retries": 2,
                            },
                        }
                    }
                },
            }
        # Docker published ports require the lab server to bind container interfaces.
        source = (
            (RUNTIME / "tests/integration/smoke_http.py")
            .read_text()
            .replace('("127.0.0.1", args.port)', '("0.0.0.0", args.port)')
        )
        (lab.folder / "smoke.py").write_text(source)
        for item in revisions.values():
            item["compose"]["services"]["web"]["volumes"][
                1
            ] = f"{lab.folder / 'smoke.py'}:/smoke.py:ro"
        folder = lab.folder / "deploy"
        atomic_json(folder / "a.revision.json", revisions["old"])

        class LocalTargets(DockerWebBackend):
            state = "healthy"

            def _route(self, node, action):
                if action == "drain":
                    self.state = "unused"
                if action == "register":
                    self.state = "healthy"
                return {"accepted": True, "state": self.state, "inflight": 0}

            def _wait(self, predicate):
                for _ in range(8):
                    if predicate():
                        return
                    time.sleep(0.5)
                raise UnsafeTransition("local readiness failed")

        backend = LocalTargets(
            {
                "a": {
                    "project": lab.project,
                    "readiness_url": f"http://127.0.0.1:{port}/ready",
                    "revisions": revisions,
                }
            },
            folder,
        )
        try:
            backend.replace_web("a", "old")
            backend.wait_ready("a", "old")
            rollout(["a"], "new", backend)
            with self.assertRaises(UnsafeTransition):
                rollout(["a"], "bad", backend)
            backend.wait_ready("a", "new")
            self.assertEqual(backend.snapshot("a")["version"], "new")
        finally:
            if (folder / "a.json").exists():
                lab.docker(
                    "compose",
                    "--project-name",
                    lab.project,
                    "--file",
                    folder / "a.json",
                    "down",
                )


if __name__ == "__main__":
    unittest.main()
