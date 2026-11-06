"""Fail-closed operations; adapters must supply observed execution evidence."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import http.client
import re
import uuid


class UnsafeTransition(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def local_lock(path):
    """Host-only exclusion, never a replacement for a distributed lease."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UnsafeTransition("timer is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class FenceEvidence:
    owner: str
    generation: str
    epoch: int
    observed_at: float
    containers_stopped: bool
    drivers_absent: bool
    executors_absent: bool
    cluster_idle: bool
    all_masters_observed: bool
    all_workers_observed: bool

    def validate(self, owner, generation, epoch, now):
        flags = (
            self.containers_stopped,
            self.drivers_absent,
            self.executors_absent,
            self.cluster_idle,
            self.all_masters_observed,
            self.all_workers_observed,
        )
        if any(type(flag) is not bool for flag in flags):
            raise UnsafeTransition("fence observations must be booleans")
        if (self.owner, self.generation, self.epoch) != (owner, generation, epoch):
            raise UnsafeTransition(
                "fence evidence belongs to another owner/generation/epoch"
            )
        if not 0 <= now - self.observed_at <= 10:
            raise UnsafeTransition("stale or future fence evidence")
        if not all(
            [
                self.containers_stopped,
                self.drivers_absent,
                self.executors_absent,
                self.cluster_idle,
                self.all_masters_observed,
                self.all_workers_observed,
            ]
        ):
            raise UnsafeTransition(
                "old owner termination / whole-cluster idle is unconfirmed"
            )


class ActiveGroup:
    """Persist old owner until complete proof, then CAS the authoritative lease.

    lease.read()/lease.cas(previous, next) must be linearizable in production.
    State-file locking here only excludes local timer/supervisor races.
    """

    def __init__(self, state, generation, owner, lease, execution, clock=time.time):
        self.state, self.generation, self.owner = Path(state), generation, owner
        self.lease, self.execution, self.clock = lease, execution, clock

    def now(self):
        return self.lease.now() if hasattr(self.lease, "now") else self.clock()

    def acquire(self):
        with local_lock(str(self.state) + ".lock"):
            previous = self.lease.read()
            now = self.now()
            if previous and previous["expires_at"] > now:
                raise UnsafeTransition("previous owner lease is still active")
            if previous:
                # Even an expired lease is not proof that containers/executors stopped.
                if previous["generation"] != self.generation:
                    raise UnsafeTransition(
                        "mixed-generation lease requires operator reconciliation"
                    )
                self.execution.stop_group(previous)
                proof = self.execution.observe(previous)
                proof.validate(
                    previous["owner"], self.generation, previous["epoch"], self.clock()
                )
                now = self.now()
            elif hasattr(self.execution, "assert_initial_idle"):
                self.execution.assert_initial_idle(self.owner, self.generation)
                now = self.now()
            new = {
                "owner": self.owner,
                "generation": self.generation,
                "epoch": previous["epoch"] + 1 if previous else 1,
                "expires_at": now + 15,
            }
            if previous and previous["generation"] != self.generation:
                raise UnsafeTransition(
                    "mixed-generation lease requires operator reconciliation"
                )
            if not self.lease.cas(previous, new):
                raise UnsafeTransition("lease changed during takeover")
            atomic_json(
                self.state, {"phase": "owned", "lease": new, "old_owner": previous}
            )
            return new

    def tick(self, token):
        with local_lock(str(self.state) + ".lock"):
            try:
                current = self.lease.read()
                now = self.now()
            except Exception as exc:
                self._fence(token)
                raise UnsafeTransition(
                    "lease backend unavailable; group self-fenced"
                ) from exc
            if current != token or token["expires_at"] <= now:
                self._fence(token)
                raise UnsafeTransition(
                    "lease lost; group fenced or takeover remains blocked"
                )
            renewed = {**token, "expires_at": now + 15}
            try:
                accepted = self.lease.cas(token, renewed)
            except Exception as exc:
                self._fence(token)
                raise UnsafeTransition(
                    "lease renewal unavailable; group self-fenced"
                ) from exc
            if not accepted:
                self._fence(token)
                raise UnsafeTransition("lease renewal rejected")
            saved = (
                json.loads(self.state.read_text())
                if self.state.exists()
                else {"old_owner": None}
            )
            atomic_json(self.state, {**saved, "phase": "owned", "lease": renewed})
            return renewed

    def _fence(self, token):
        atomic_json(
            self.state, {"phase": "fencing", "lease": token, "old_owner": token}
        )
        # execution stops actual named containers, kills observed drivers/executors,
        # then queries all masters/workers. A Docker CLI exit alone is insufficient.
        self.execution.stop_group(token)
        proof = self.execution.observe(token)
        proof.validate(token["owner"], self.generation, token["epoch"], self.clock())
        atomic_json(
            self.state,
            {
                "phase": "fenced",
                "lease": token,
                "old_owner": token,
                "evidence": proof.__dict__,
            },
        )


def backup(command, destination, verify):
    """Stream pg_dump/mongodump to a new file; failed output never replaces a backup."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with local_lock(str(destination.parent / ".backup.lock")):
        if destination.exists():
            raise UnsafeTransition("backup destination already exists")
        fd, name = tempfile.mkstemp(prefix=".partial-", dir=destination.parent)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                subprocess.run(
                    command, stdout=out, stderr=subprocess.PIPE, check=True, timeout=900
                )
                out.flush()
                os.fsync(out.fileno())
            if path.stat().st_size == 0 or not verify(path):
                raise UnsafeTransition("backup verification failed")
            os.replace(path, destination)
            atomic_json(
                str(destination) + ".receipt.json",
                {
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                    "bytes": destination.stat().st_size,
                    "verified": True,
                    "scope": "local archive; S3 upload and restore verification are separate",
                },
            )
        finally:
            path.unlink(missing_ok=True)
    return destination


def rollout(nodes, revision, backend, *, receipt_path=None):
    """Serialize web replacement; crash/ambiguous receipts require reconciliation.

    Real DockerWebBackend binds a durable deployment folder. Lightweight unit
    adapters without a folder use an ephemeral receipt, never production state.
    """
    if not nodes or len(set(nodes)) != len(nodes):
        raise UnsafeTransition("rollout requires distinct target nodes")
    if receipt_path is None and hasattr(backend, "folder"):
        receipt_path = Path(backend.folder) / "rollout-state.json"
    with tempfile.TemporaryDirectory(prefix="mlo-rollout-") as temporary:
        path = Path(receipt_path) if receipt_path else Path(temporary) / "rollout.json"
        with local_lock(str(path) + ".lock"):
            if path.exists():
                saved = json.loads(path.read_text())
                if saved.get("phase") not in {
                    "complete",
                    "rollback_complete",
                    "reconciled",
                }:
                    raise UnsafeTransition(
                        "pending or ambiguous rollout requires explicit observed reconciliation"
                    )
            receipt = {
                "operation_id": uuid.uuid4().hex,
                "nodes": list(nodes),
                "revision": revision,
                "phase": "pending",
                "previous": {},
                "started_at": time.time(),
            }
            atomic_json(path, receipt)

            def record(**values):
                receipt.update(values)
                atomic_json(path, receipt)

            try:
                _rollout(nodes, revision, backend, record)
            except BaseException:
                # SIGKILL cannot write here; the previous pending phase survives.
                history = path.parent / (
                    "rollout-" + receipt["operation_id"] + ".receipt.json"
                )
                atomic_json(history, receipt)
                raise
            atomic_json(
                path.parent / ("rollout-" + receipt["operation_id"] + ".receipt.json"),
                receipt,
            )
            return receipt


def _rollout(nodes, revision, backend, record):
    """Web-only serial replacement with snapshot restore, including partial failure."""
    updated = []
    try:
        for node in nodes:
            previous = backend.snapshot(node)
            backend.check_migration(node, previous, revision)
            # Track before drain so a partial drain failure can be recovered.
            updated.append((node, previous))
            record(phase="replacing", node=node, previous=dict(updated))
            backend.drain(node)
            backend.wait_drained(node)
            backend.replace_web(node, revision)
            backend.wait_ready(node, revision)
            backend.register(node)
            backend.wait_healthy(node)
        record(phase="complete", finished_at=time.time())
    except Exception as original:
        failures = []
        for node, previous in reversed(updated):
            try:
                backend.drain(node)
                backend.wait_drained(node)
                backend.replace_web(node, previous)
                backend.wait_ready(node, previous)
                backend.register(node)
                backend.wait_healthy(node)
            except Exception:
                failures.append(node)
                try:
                    backend.drain(node)
                except Exception:
                    pass
        if failures:
            record(
                phase="recovery_required",
                failed_nodes=failures,
                finished_at=time.time(),
            )
            raise UnsafeTransition(
                "rollback incomplete; keep failed targets drained: "
                + ",".join(failures)
            ) from original
        record(phase="rollback_complete", finished_at=time.time())
        raise UnsafeTransition(
            "rollout failed; previous web revisions restored"
        ) from original


def stop_stateful(node, backend, *, receipt_path=None):
    """Planned stop only: quiesce -> dump -> independent copy -> restore -> stop.

    Never claims crash, forced termination or EC2 loss can run a final backup.
    Adapters must return observations tied to the exact backup digest/barrier.
    """
    if receipt_path is None and hasattr(backend, "folder"):
        receipt_path = Path(backend.folder) / (node + "-stop-state.json")
    with tempfile.TemporaryDirectory(prefix="mlo-stop-") as temporary:
        path = Path(receipt_path) if receipt_path else Path(temporary) / "state.json"
        with local_lock(str(path) + ".lock"):
            if (
                path.exists()
                and json.loads(path.read_text()).get("phase") != "complete"
            ):
                raise UnsafeTransition("ambiguous planned stop requires reconciliation")
            record = {
                "operation_id": uuid.uuid4().hex,
                "node": node,
                "phase": "quiescing",
                "scope": "planned stop only; no forced-stop guarantee",
            }
            atomic_json(path, record)
            stop_attempted = False
            try:
                barrier = backend.quiesce(node)
                if (
                    barrier.get("node") != node
                    or barrier.get("writes_blocked") is not True
                    or not barrier.get("barrier_id")
                ):
                    raise UnsafeTransition("write quiescence unconfirmed")
                record.update(phase="backup", barrier=barrier)
                atomic_json(path, record)
                archive = backend.backup(node)
                digest = archive.get("sha256", "")
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or archive.get("verified") is not True
                    or archive.get("barrier_id") != barrier["barrier_id"]
                    or not archive.get("location")
                ):
                    raise UnsafeTransition(
                        "backup not verified against quiescence barrier"
                    )
                copied = backend.copy_backup(node, archive)
                if (
                    copied.get("sha256") != digest
                    or not copied.get("location")
                    or copied["location"] == archive["location"]
                ):
                    raise UnsafeTransition(
                        "independent backup copy missing or mismatched"
                    )
                verified = backend.verify_copy(node, copied)
                restored = backend.verify_restore(node, copied)
                if any(
                    value.get("verified") is not True or value.get("sha256") != digest
                    for value in (verified, restored)
                ):
                    raise UnsafeTransition(
                        "independent copy/restore verification failed"
                    )
                record.update(
                    phase="stop_pending",
                    backup=archive,
                    independent_copy=copied,
                    copy_verification=verified,
                    restore_verification=restored,
                )
                atomic_json(path, record)
                stop_attempted = True
                backend.stop(node)
                if backend.observe_stopped(node) is not True:
                    raise UnsafeTransition("stateful stop unconfirmed")
                record.update(phase="complete", finished_at=time.time())
                atomic_json(path, record)
                atomic_json(
                    path.parent
                    / (node + "-stop-" + record["operation_id"] + ".receipt.json"),
                    record,
                )
                return record
            except Exception as error:
                record.update(
                    phase="stop_ambiguous" if stop_attempted else "blocked",
                    error_type=type(error).__name__,
                )
                atomic_json(path, record)
                raise UnsafeTransition(
                    "planned stop blocked or unconfirmed; inspect receipt"
                ) from error


@dataclass(frozen=True)
class Request:
    request_id: str
    deadline: float
    revision: str
    candidates: tuple[str, ...]
    payload: dict

    def body(self):
        # Allowlisted business envelope only: never forward DB/AWS credentials.
        if not self.request_id or not self.revision or not self.candidates:
            raise UnsafeTransition("invalid model envelope")
        if set(self.payload) - {"prompt", "context"}:
            raise UnsafeTransition("unsupported model payload fields")
        return json.dumps(
            {**self.__dict__, "candidates": list(self.candidates)}, sort_keys=True
        ).encode()


def request_model(request, gateways, token, clock=time.time, ca_file=None):
    """Sequential failover preserves bytes/ID/deadline; home must deduplicate IDs."""
    body = request.body()
    from .relay import tls_opener

    opener = tls_opener(ca_file)
    for gateway in gateways:
        remaining = request.deadline - clock()
        if remaining <= 0:
            raise TimeoutError("deadline exceeded; GPU slot release is unconfirmed")
        req = urllib.request.Request(
            gateway,
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "X-Request-ID": request.request_id,
            },
            method="POST",
        )
        try:
            with opener.open(req, timeout=min(remaining, 5)) as response:
                result = json.loads(response.read(1024 * 1024))
                pending = response.status == 202
            while pending:
                remaining = request.deadline - clock()
                if remaining <= 0:
                    raise TimeoutError(
                        "pending model request; resource release unconfirmed"
                    )
                time.sleep(min(0.03, remaining))
                base = gateway.removesuffix("/requests").rstrip("/")
                lookup = urllib.request.Request(
                    base
                    + "/requests/"
                    + urllib.parse.quote(request.request_id, safe=""),
                    headers={"Authorization": "Bearer " + token},
                    method="GET",
                )
                with opener.open(lookup, timeout=min(remaining, 5)) as response:
                    result = json.loads(response.read(1024 * 1024))
                    pending = response.status == 202
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in {429, 503}:
                continue
            raise UnsafeTransition(
                "model service authentication/request rejected"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.RemoteDisconnected,
        ):
            continue
        if clock() > request.deadline:
            raise TimeoutError("late result; GPU slot release is unconfirmed")
        if result.get("state") in {"cancelled", "expired"}:
            raise UnsafeTransition("model request has no usable final result")
        if (
            result.get("request_id") != request.request_id
            or result.get("revision") != request.revision
        ):
            raise UnsafeTransition("mismatched model response")
        if result.get("candidate") not in request.candidates:
            raise UnsafeTransition("model proposed an unapproved candidate")
        return result
    raise UnsafeTransition(
        "model unavailable; acceptance/cancellation/GPU status must be reconciled with original ID"
    )


def model_status(request_id, gateways, token, *, cancel=False, ca_file=None):
    """Reconcile original ID after ambiguous acceptance; never invent a new ID."""
    from .relay import tls_opener

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", request_id):
        raise UnsafeTransition("invalid model request ID")
    opener = tls_opener(ca_file)
    for gateway in gateways:
        origin = gateway.removesuffix("/requests").rstrip("/")
        request = urllib.request.Request(
            origin + "/requests/" + request_id,
            method="DELETE" if cancel else "GET",
            headers={"Authorization": "Bearer " + token},
        )
        try:
            with opener.open(request, timeout=3) as response:
                value = json.loads(response.read(1024 * 1024))
                if value.get("request_id") != request_id:
                    raise UnsafeTransition("lookup response ID mismatch")
                return value
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code not in {429, 503}:
                raise UnsafeTransition("model lookup/cancel rejected") from exc
        except (urllib.error.URLError, OSError, http.client.RemoteDisconnected):
            continue
    raise UnsafeTransition("model lookup/cancel unavailable; GPU release unconfirmed")


def cancel_status(value):
    """Cancellation acknowledgement and resource release are separate observations."""
    if value.get("state") not in {"requested", "cancelled", "completed"}:
        raise UnsafeTransition("unrecognized cancellation state")
    return {
        "acknowledged": value["state"] in {"cancelled", "completed"},
        "gpu_slot_released": value.get("gpu_slot_released") is True,
    }
