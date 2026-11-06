"""PostgreSQL transactional lease for the existing application database.

Call initialize() during an approved schema installation. DSNs are accepted from
protected bindings, never emitted in receipts. All SQL values are parameters.
"""

import math
import re

from .operations import UnsafeTransition


def valid_token(token):
    if not isinstance(token, dict) or set(token) != {
        "owner",
        "generation",
        "epoch",
        "expires_at",
    }:
        raise UnsafeTransition("invalid lease token")
    if any(
        not isinstance(token[k], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", token[k])
        for k in ("owner", "generation")
    ):
        raise UnsafeTransition("invalid lease identity")
    if (
        type(token["epoch"]) is not int
        or token["epoch"] < 1
        or type(token["expires_at"]) not in (float, int)
        or not math.isfinite(token["expires_at"])
    ):
        raise UnsafeTransition("invalid lease epoch/expiry")


class PostgresLease:
    def __init__(self, dsn, group="control-active", *, lab=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", group):
            raise UnsafeTransition("invalid lease group")
        self.dsn, self.group = dsn, group
        from psycopg.conninfo import conninfo_to_dict

        settings = conninfo_to_dict(dsn)
        if lab:
            if settings.get("host") not in {"localhost", "127.0.0.1", "::1"}:
                raise UnsafeTransition("insecure lease lab must bind loopback")
        elif settings.get("sslmode") != "verify-full":
            raise UnsafeTransition(
                "production lease requires verify-full PostgreSQL TLS"
            )

    def _connect(self):
        import psycopg

        return psycopg.connect(
            self.dsn,
            connect_timeout=3,
            options="-c statement_timeout=3000 -c lock_timeout=2000",
        )

    def initialize(self):
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS mlo_execution_lease_v1 (
                group_name text PRIMARY KEY, owner text NOT NULL,
                generation text NOT NULL, epoch bigint NOT NULL CHECK(epoch > 0),
                expires_at double precision NOT NULL)""")

    def now(self):
        with self._connect() as connection:
            return float(
                connection.execute(
                    "SELECT extract(epoch FROM clock_timestamp())::float8"
                ).fetchone()[0]
            )

    def read(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner,generation,epoch,expires_at FROM mlo_execution_lease_v1 WHERE group_name=%s",
                (self.group,),
            ).fetchone()
        return (
            dict(zip(("owner", "generation", "epoch", "expires_at"), row))
            if row
            else None
        )

    def cas(self, previous, successor):
        valid_token(successor)
        if previous is not None:
            valid_token(previous)
        with self._connect() as connection:
            # One transaction serializes missing-row races as well as renew/takeover.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (self.group,)
            )
            row = connection.execute(
                "SELECT owner,generation,epoch,expires_at FROM mlo_execution_lease_v1 WHERE group_name=%s FOR UPDATE",
                (self.group,),
            ).fetchone()
            current = (
                dict(zip(("owner", "generation", "epoch", "expires_at"), row))
                if row
                else None
            )
            now = float(
                connection.execute(
                    "SELECT extract(epoch FROM clock_timestamp())::float8"
                ).fetchone()[0]
            )
            if current != previous or not now < successor["expires_at"] <= now + 60:
                return False
            if previous:
                if successor["generation"] != previous["generation"]:
                    return False
                renew = (
                    successor["owner"] == previous["owner"]
                    and successor["epoch"] == previous["epoch"]
                )
                if renew:
                    if (
                        previous["expires_at"] <= now
                        or successor["expires_at"] <= previous["expires_at"]
                    ):
                        return False
                elif (
                    previous["expires_at"] > now
                    or successor["epoch"] != previous["epoch"] + 1
                ):
                    return False
            elif successor["epoch"] != 1:
                return False
            connection.execute(
                """INSERT INTO mlo_execution_lease_v1(group_name,owner,generation,epoch,expires_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(group_name) DO UPDATE SET
                owner=excluded.owner,generation=excluded.generation,epoch=excluded.epoch,expires_at=excluded.expires_at""",
                (
                    self.group,
                    successor["owner"],
                    successor["generation"],
                    successor["epoch"],
                    successor["expires_at"],
                ),
            )
            return True
