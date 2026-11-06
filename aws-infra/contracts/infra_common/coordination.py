"""A/B/C operation exclusion with CAS, durable epochs and explicit uncertain outcomes.

Expired held operations cannot be taken over automatically: an external request may
still be in flight. Reconciliation is an explicit operator workflow, not a TTL trick.
"""

from contextlib import contextmanager
import hashlib
import json
import math
import re
import threading
import time
import uuid


class CoordinationError(ValueError):
    pass


def require(value, reason):
    if not value:
        raise CoordinationError(reason)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def scope_key(scope):
    require(
        set(scope)
        == {"project", "environment", "generation", "account_ids", "state_keys"},
        "invalid operation scope",
    )
    require(
        scope["project"] == "mlo-p4"
        and scope["environment"] == "dev"
        and scope["generation"],
        "invalid scope",
    )
    accounts = {"encore1", "encore2", "encore3", "encore4", "encore5"}
    require(
        set(scope["account_ids"]) == accounts
        and len(set(scope["account_ids"].values())) == 5
        and all(
            isinstance(v, str) and re.fullmatch(r"[0-9]{12}", v)
            for v in scope["account_ids"].values()
        )
        and scope["state_keys"]
        == {
            k: f"dev/{k}/terraform.tfstate"
            for k in accounts | {"bootstrap", "connectivity"}
        },
        "incomplete physical scope",
    )
    # A changed generation must not obtain another lock for the same physical resources.
    return hashlib.sha256(
        canonical({k: v for k, v in scope.items() if k != "generation"})
    ).hexdigest()


class S3CAS:
    """Injected client, exact bucket/key, conditional writes, no client/profile discovery.

    No DeleteObject: the final record preserves the monotonically increasing epoch.
    Failed/unknown writes propagate. No automatic retries of an ambiguous operation.
    """

    def __init__(self, client, bucket, key):
        require(
            isinstance(bucket, str)
            and bucket
            and key.startswith("operations/")
            and ".." not in key,
            "operation storage must be separate from state/lockfile keys",
        )
        self.client, self.bucket, self.key = client, bucket, key

    def read(self):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except self.client.exceptions.NoSuchKey:
            return None, None
        try:
            raw = response["Body"].read(65537)
            require(len(raw) <= 65536, "operation record exceeds bound")
            value = json.loads(raw)
        finally:
            response["Body"].close()
        require(
            response.get("ETag")
            and response.get("VersionId") not in (None, "", "null"),
            "versioned operation storage required",
        )
        return value, response["ETag"]

    def cas(self, previous, value):
        args = {"IfNoneMatch": "*"} if previous is None else {"IfMatch": previous}
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=canonical(value),
            ContentType="application/json",
            ServerSideEncryption="AES256",
            **args,
        )
        require(
            response.get("ETag")
            and response.get("VersionId") not in (None, "", "null"),
            "versioned CAS receipt missing",
        )
        return response["ETag"]


class OperationLease:
    def __init__(self, store, scope, owner, operation, *, ttl=30, clock=time.time):
        self.key = scope_key(scope)
        require(
            operation in {"create", "deploy", "destroy"}
            and isinstance(owner, str)
            and owner,
            "invalid owner/operation",
        )
        require(
            type(ttl) in (float, int) and math.isfinite(ttl) and 1 <= ttl <= 60,
            "invalid lease TTL",
        )
        self.store, self.scope, self.owner, self.operation, self.ttl, self.clock = (
            store,
            scope,
            owner,
            operation,
            ttl,
            clock,
        )
        self.token = None
        self.failure = None
        self.mutex = threading.RLock()

    def acquire(self):
        with self.mutex:
            previous, etag = self.store.read()
            require(
                previous is not None,
                "operation record missing; explicit bootstrap initialization required",
            )
            self._validate(previous)
            require(
                previous["state"] == "complete",
                "held/ambiguous operation requires reconciliation, even after expiry",
            )
            now = self.clock()
            epoch = previous["epoch"] + 1
            token = {
                "schema_version": "infra-operation-v1",
                "scope_key": self.key,
                "scope": self.scope,
                "owner": self.owner,
                "operation": self.operation,
                "operation_id": uuid.uuid4().hex,
                "epoch": epoch,
                "issued_at": now,
                "expires_at": now + self.ttl,
                "state": "held",
                "receipt_sha256": None,
            }
            self.store.cas(etag, token)
            self.token = token
            return dict(token)

    def initialize(self):
        """One explicit conditional create, without a missing-key GET.

        S3 can return AccessDenied for an absent object to a principal without
        ListBucket. IfNoneMatch is the authoritative atomic existence check;
        no broad bucket listing is needed merely to bootstrap this exact key.
        A lost create response is uncertain and must never be auto-retried.
        """
        now = self.clock()
        self.store.cas(
            None,
            {
                "schema_version": "infra-operation-v1",
                "scope_key": self.key,
                "scope": self.scope,
                "owner": self.owner,
                "operation": "create",
                "operation_id": uuid.uuid4().hex,
                "epoch": 1,
                "issued_at": now,
                "expires_at": now + self.ttl,
                "state": "complete",
                "receipt_sha256": hashlib.sha256(
                    b"explicit-bootstrap-initialization"
                ).hexdigest(),
            },
        )

    def _validate(self, record):
        require(
            set(record)
            == {
                "schema_version",
                "scope_key",
                "scope",
                "owner",
                "operation",
                "operation_id",
                "epoch",
                "issued_at",
                "expires_at",
                "state",
                "receipt_sha256",
            },
            "invalid operation record",
        )
        require(
            record["schema_version"] == "infra-operation-v1"
            and record["scope_key"] == self.key
            and scope_key(record["scope"]) == self.key
            and type(record["epoch"]) is int
            and record["epoch"] > 0
            and record["state"] in {"held", "complete", "ambiguous"},
            "wrong operation scope/epoch/state",
        )
        require(
            all(
                type(record[k]) in (float, int) and math.isfinite(record[k])
                for k in ("issued_at", "expires_at")
            ),
            "invalid operation clock",
        )

    def _current(self):
        require(
            self.token is not None and self.failure is None,
            "operation lease not owned or lost",
        )
        record, etag = self.store.read()
        require(record is not None, "operation record disappeared")
        self._validate(record)
        require(
            all(
                record[k] == self.token[k]
                for k in ("scope", "owner", "operation", "operation_id", "epoch")
            )
            and record["state"] == "held",
            "operation fencing token replaced",
        )
        require(
            record["issued_at"] - 1 <= self.clock() < record["expires_at"] - 0.25,
            "operation lease expired/clock skew",
        )
        return record, etag

    def check(self):
        with self.mutex:
            return dict(self._current()[0])

    def renew(self):
        with self.mutex:
            record, etag = self._current()
            record = {**record, "expires_at": self.clock() + self.ttl}
            self.store.cas(etag, record)
            self.token = record

    def finish(self, receipt):
        with self.mutex:
            record, etag = self._current()
            self.store.cas(
                etag,
                {
                    **record,
                    "state": "complete",
                    "receipt_sha256": hashlib.sha256(canonical(receipt)).hexdigest(),
                },
            )
            self.token = None

    @contextmanager
    def hold(self):
        self.acquire()
        stopped = threading.Event()

        def heartbeat():
            while not stopped.wait(self.ttl / 4):
                try:
                    self.renew()
                except Exception as exc:
                    self.failure = type(exc).__name__
                    return

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield self
            self.check()
            self.finish(
                {"status": "complete", "operation": self.operation, "scope": self.scope}
            )
        except BaseException:
            # Leave held, or persist ambiguous if still our exact CAS record. Never release
            # uncertainty or overwrite a different owner's record to make retries succeed.
            try:
                record, etag = self.store.read()
                if (
                    record
                    and self.token
                    and record["operation_id"] == self.token["operation_id"]
                ):
                    self.store.cas(etag, {**record, "state": "ambiguous"})
            except Exception:
                pass  # held record remains fail-closed; original failure is propagated.
            raise
        finally:
            stopped.set()
            worker.join(timeout=2)


def require_operation(guard, operation):
    require(
        guard is not None and getattr(guard, "operation", None) == operation,
        "accepted common operation guard required",
    )
    return guard.check()


class MaintenanceBarrier:
    def __init__(self, store, scope):
        self.store, self.scope, self.key = store, scope, scope_key(scope)

    def check(self):
        record, _ = self.store.read()
        OperationLease(self.store, self.scope, "barrier-reader", "deploy")._validate(
            record or {}
        )
        require(
            record is not None
            and record["scope_key"] == self.key
            and record["scope"] == self.scope,
            "maintenance scope unknown",
        )
        if record["state"] == "held":
            require(
                record["issued_at"] - 1 <= time.time() < record["expires_at"] - 0.25,
                "maintenance lease expired/clock skew",
            )
        require(
            record["state"] == "complete"
            or record["state"] == "held"
            and record["operation"] == "deploy",
            "maintenance inhibits new workloads/takeover",
        )
