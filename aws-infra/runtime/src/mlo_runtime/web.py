"""Web-only local Docker deployment backend for an accepted per-host payload.

Target deregistration is a hash-verified fixed foundation hook, not shell text.
Invoke on the target host through the accepted SSM artifact entrypoint.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.request

from .operations import UnsafeTransition, atomic_json


class DockerWebBackend:
    def __init__(self, bindings, folder, clock=time.monotonic, sleep=time.sleep):
        self.bindings, self.folder = bindings, Path(folder)
        self.clock, self.sleep = clock, sleep

    def _run(self, argv):
        return subprocess.run(argv, check=True, capture_output=True, timeout=60).stdout

    def _compose(self, node, *args):
        return self._run(
            [
                "docker",
                "compose",
                "--project-name",
                self.bindings[node]["project"],
                "--file",
                str(self.folder / (node + ".json")),
                *args,
            ]
        )

    def _revision(self, node, revision):
        item = (
            revision
            if isinstance(revision, dict)
            else self.bindings[node]["revisions"][revision]
        )
        config = item["compose"]
        if set(config["services"]) != {"web"}:
            raise UnsafeTransition("web deployment cannot include stateful services")
        image = config["services"]["web"]["image"]
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
            raise UnsafeTransition("immutable web image digest required")
        if not item.get("migration_compatible") or not item.get("version"):
            raise UnsafeTransition(
                "migration compatibility and version must be accepted"
            )
        return item

    def snapshot(self, node):
        previous = json.loads((self.folder / (node + ".revision.json")).read_text())
        self._revision(node, previous)
        return previous

    def check_migration(self, node, old, new):
        self._revision(node, new)

    def _route(self, node, action):
        item = self.bindings[node]["target_hook"]
        hook = Path(item["path"])
        if (
            not hook.is_absolute()
            or hashlib.sha256(hook.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise UnsafeTransition("target-control hook checksum mismatch")
        return json.loads(self._run([str(hook), "--node", node, "--action", action]))

    def drain(self, node):
        if self._route(node, "drain").get("accepted") is not True:
            raise UnsafeTransition("target exclusion rejected")

    def _wait(self, predicate):
        deadline = self.clock() + 60
        while self.clock() < deadline:
            if predicate():
                return
            self.sleep(1)
        raise UnsafeTransition("deployment readiness/drain deadline exceeded")

    def wait_drained(self, node):
        def ready():
            observed = self._route(node, "status")
            return observed.get("state") == "unused" and observed.get("inflight") == 0

        self._wait(ready)

    def replace_web(self, node, revision):
        item = self._revision(node, revision)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Save old accepted revision independently; compose up only targets web.
        atomic_json(self.folder / (node + ".json"), item["compose"])
        self._compose(node, "up", "-d", "--no-deps", "web")
        atomic_json(self.folder / (node + ".revision.json"), item)

    def wait_ready(self, node, revision):
        expected = self._revision(node, revision)

        def ready():
            try:
                ids = self._compose(node, "ps", "-q", "web").decode().split()
                if len(ids) != 1:
                    return False
                actual = json.loads(self._run(["docker", "inspect", ids[0]]))[0]
                if (
                    actual["Config"]["Image"]
                    != expected["compose"]["services"]["web"]["image"]
                ):
                    return False
                if actual["State"].get("Health", {}).get("Status") != "healthy":
                    return False
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(
                    self.bindings[node]["readiness_url"], timeout=3
                ) as response:
                    return (
                        response.status == 200
                        and json.loads(response.read(65536)).get("revision")
                        == expected["version"]
                    )
            except (OSError, ValueError, subprocess.SubprocessError):
                return False

        self._wait(ready)

    def register(self, node):
        if self._route(node, "register").get("accepted") is not True:
            raise UnsafeTransition("target registration rejected")

    def wait_healthy(self, node):
        self._wait(lambda: self._route(node, "status").get("state") == "healthy")
