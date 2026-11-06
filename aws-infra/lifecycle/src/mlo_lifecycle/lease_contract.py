"""Single-authority reference state machine; NOT a distributed lock backend.

Production consumers must implement atomic CAS, durable epochs and fence every effect.
Only contract tests use this model; C execution continues to advertise local flock only.
"""

from .model import digest, need, timestamp


class LeaseContract:
    def __init__(self):
        self.rows = {}

    @staticmethod
    def scope_key(scope):
        # Generation is an ownership assertion, not a separate lock for the same resources.
        return digest({k: scope[k] for k in ("resource_scope", "environment")})

    def acquire(self, scope, owner, operation, now, ttl, *, expected_epoch):
        need(set(scope) == {"resource_scope", "environment", "generation"}
             and all(isinstance(v, str) and v for v in scope.values()), "invalid common scope")
        need(operation in {"create", "deploy", "destroy"} and isinstance(owner, str) and owner and timestamp(now)
             and timestamp(ttl) and 0 < ttl <= 60, "invalid lease request")
        key = self.scope_key(scope)
        previous = self.rows.get(key)
        epoch = previous["epoch"] if previous else 0
        need(type(expected_epoch) is int and expected_epoch == epoch and (previous is None or now >= previous["expiry"]),
             "CAS conflict/live owner")
        lease = {**scope, "owner": owner, "operation": operation, "epoch": epoch + 1,
                 "fencing_token": epoch + 1, "issued_at": now, "expiry": now + ttl}
        self.rows[key] = lease
        return dict(lease)

    def check(self, lease, now):
        key = self.scope_key(lease)
        need(self.rows.get(key) == lease and lease["issued_at"] <= now < lease["expiry"], "expired/stale fence")

    def renew(self, lease, now, ttl):
        self.check(lease, now)
        need(timestamp(ttl) and 0 < ttl <= 60, "invalid renewal")
        key = self.scope_key(lease)
        self.rows[key] = {**lease, "expiry": now + ttl}
        return dict(self.rows[key])
