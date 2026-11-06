"""Observed Docker/Spark fencing adapter; never concludes from CLI parent exit."""

from dataclasses import dataclass
import json
import hashlib
import math
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.request

from .operations import FenceEvidence, UnsafeTransition


class FixedCommandExecution:
    """Hash-bound remote-host protocol, never arbitrary shell text.

    Each owner has its own trusted adapter. Production adapter must perform SSM
    on that exact old host; local tests use real subprocesses instead. Adapter
    stdout is one bounded JSON receipt; credentials and shell fragments are not
    parameters. Master/worker evidence must enumerate the entire accepted set.
    """

    def __init__(self, binding):
        self.binding = binding
        if len(set(binding["masters"])) != 3 or len(set(binding["workers"])) != 3:
            raise UnsafeTransition("all three masters and workers must be bound")

    def _call(self, token, operation):
        from .lease import valid_token

        valid_token(token)
        item = self.binding["owners"].get(token["owner"])
        if item is None:
            raise UnsafeTransition("old host has no approved fencing adapter")
        path = Path(item["path"])
        if (
            not path.is_absolute()
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise UnsafeTransition("remote adapter identity/checksum mismatch")
        if item.get("target") != token["owner"]:
            raise UnsafeTransition("remote adapter target mismatch")
        argv = [
            str(path),
            "--operation",
            operation,
            "--owner",
            token["owner"],
            "--generation",
            token["generation"],
            "--epoch",
            str(token["epoch"]),
        ]
        response = subprocess.run(argv, check=True, capture_output=True, timeout=20)
        if len(response.stdout) > 1024 * 1024:
            raise UnsafeTransition("fencing receipt too large")
        receipt = json.loads(response.stdout)
        if type(receipt.get("epoch")) is not int:
            raise UnsafeTransition("remote receipt epoch must be an integer")
        if (
            receipt.get("owner"),
            receipt.get("generation"),
            receipt.get("epoch"),
            receipt.get("operation"),
        ) != (token["owner"], token["generation"], token["epoch"], operation):
            raise UnsafeTransition("remote receipt identity mismatch")
        if receipt.get("accepted") is not True:
            raise UnsafeTransition("remote operation rejected")
        return receipt

    def stop_group(self, token):
        return self._call(token, "stop")

    def assert_initial_idle(self, owner, generation):
        token = {
            "owner": owner,
            "generation": generation,
            "epoch": 1,
            "expires_at": time.time() + 15,
        }
        self.observe(token).validate(owner, generation, 1, time.time())

    def ensure_started(self, token):
        receipt = self._call(token, "start")
        if receipt.get("running") is not True:
            raise UnsafeTransition("group did not start")

    def observe(self, token):
        receipt = self._call(token, "observe")
        return self._proof(receipt)

    def _proof(self, receipt):
        if set(receipt.get("masters", [])) != set(self.binding["masters"]) or set(
            receipt.get("workers", [])
        ) != set(self.binding["workers"]):
            raise UnsafeTransition("incomplete whole-cluster observation")
        evidence = receipt.get("evidence", {})
        required = set(FenceEvidence.__dataclass_fields__)
        if set(evidence) != required:
            raise UnsafeTransition("incomplete fence evidence")
        if (
            type(evidence["epoch"]) is not int
            or type(evidence["observed_at"]) not in (int, float)
            or not math.isfinite(evidence["observed_at"])
        ):
            raise UnsafeTransition("invalid fence epoch/timestamp")
        if any(
            type(evidence[k]) is not bool
            for k in required - {"owner", "generation", "epoch", "observed_at"}
        ):
            raise UnsafeTransition("invalid fencing booleans")
        return FenceEvidence(**evidence)

    def check_host(self, token, *, running):
        receipt = self._call(token, "observe")
        proof = self._proof(receipt)
        if not proof.all_masters_observed or not proof.all_workers_observed:
            raise UnsafeTransition("host health lacks complete cluster observations")
        if (proof.owner, proof.generation, proof.epoch) != (
            token["owner"],
            token["generation"],
            token["epoch"],
        ) or not 0 <= time.time() - proof.observed_at <= 10:
            raise UnsafeTransition("host health observation stale or mismatched")
        if receipt.get("running") is not running or proof.containers_stopped is running:
            raise UnsafeTransition("host execution state does not match lease role")
        return {"ready": True, "role": "active" if running else "standby"}


@dataclass
class DockerSparkExecution:
    containers: tuple[str, ...]
    masters: tuple[str, ...]
    workers: tuple[str, ...]
    ca_file: str
    client_cert: str
    client_key: str

    def _containers(self, token):
        if not self.containers or len(self.masters) != 3 or len(self.workers) != 3:
            raise UnsafeTransition(
                "complete old container set and all Spark endpoints required"
            )
        output = subprocess.run(
            ["docker", "inspect", *self.containers],
            check=True,
            capture_output=True,
            timeout=15,
            text=True,
        )
        observed = json.loads(output.stdout)
        if len(observed) != len(self.containers):
            raise UnsafeTransition("container observation incomplete")
        for item in observed:
            labels = item["Config"].get("Labels") or {}
            for key in ["owner", "generation", "epoch"]:
                if labels.get("mlo." + key) != str(token[key]):
                    raise UnsafeTransition(
                        "container identity does not match old owner token"
                    )
        return observed

    def stop_group(self, token):
        self._containers(token)
        # Stop actual containers. Failure propagates and preserves fencing/old_owner state.
        subprocess.run(
            ["docker", "stop", "--time", "30", *self.containers],
            check=True,
            capture_output=True,
            timeout=120,
        )

    def _json(self, url):
        if not url.startswith("https://"):
            raise UnsafeTransition(
                "Spark observation requires authenticated TLS endpoints"
            )
        context = ssl.create_default_context(cafile=self.ca_file)
        context.load_cert_chain(self.client_cert, self.client_key)
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        with opener.open(url, timeout=3) as response:
            return json.loads(response.read(4 * 1024 * 1024))

    def observe(self, token):
        observed = self._containers(token)
        containers_stopped = all(
            not item["State"]["Running"]
            and item["State"]["Status"] in {"exited", "created", "dead"}
            for item in observed
        )
        masters, workers = [self._json(url) for url in self.masters], [
            self._json(url) for url in self.workers
        ]
        # Missing fields fail closed instead of interpreting an unknown response as idle.
        valid_masters = all(
            set(m) >= {"status", "activeapps", "activedrivers"} for m in masters
        )
        valid_workers = all(
            set(w) >= {"executors", "drivers", "coresused"} for w in workers
        )
        one_active = sum(m.get("status") == "ALIVE" for m in masters) == 1
        return FenceEvidence(
            token["owner"],
            token["generation"],
            token["epoch"],
            time.time(),
            containers_stopped=containers_stopped,
            drivers_absent=valid_masters
            and valid_workers
            and all(not m["activeapps"] and not m["activedrivers"] for m in masters)
            and all(not w["drivers"] for w in workers),
            executors_absent=valid_workers and all(not w["executors"] for w in workers),
            cluster_idle=one_active
            and valid_workers
            and all(w["coresused"] == 0 for w in workers),
            all_masters_observed=valid_masters,
            all_workers_observed=valid_workers,
        )
