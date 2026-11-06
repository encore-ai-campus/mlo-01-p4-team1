"""Engine-specific planned stop using exact accepted container IDs and versioned copies.

No discovery, shell, volume removal or automatic restart/restore into the source.
Application writer roles, protected credentials and the restore target are inputs.
"""

import hashlib
import json
from pathlib import Path
import re
import subprocess

from .operations import UnsafeTransition, atomic_json


def need(value, reason):
    if not value:
        raise UnsafeTransition(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class VersionedBackups:
    def __init__(self, client, bucket, prefix):
        need(
            prefix.startswith("backups/") and ".." not in prefix and bucket,
            "invalid backup scope",
        )
        self.client, self.bucket, self.prefix = client, bucket, prefix.rstrip("/")

    def put(self, name, raw):
        key = self.prefix + "/" + name
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=raw,
            IfNoneMatch="*",
            ServerSideEncryption="AES256",
            ContentType="application/octet-stream",
        )
        need(
            response.get("VersionId") not in (None, "", "null"),
            "independent version receipt missing",
        )
        copied = {
            "location": "s3://" + self.bucket + "/" + key,
            "bucket": self.bucket,
            "key": key,
            "version_id": response["VersionId"],
            "sha256": sha(raw),
        }
        need(
            sha(self.get(copied)) == copied["sha256"],
            "remote copy visibility/checksum failure",
        )
        return copied

    def get(self, receipt):
        need(
            receipt["bucket"] == self.bucket
            and receipt["key"].startswith(self.prefix + "/")
            and receipt["version_id"] not in (None, "", "null"),
            "copy receipt outside backup scope",
        )
        response = self.client.get_object(
            Bucket=self.bucket, Key=receipt["key"], VersionId=receipt["version_id"]
        )
        try:
            raw = response["Body"].read(512 * 1024 * 1024 + 1)
        finally:
            response["Body"].close()
        need(
            len(raw) <= 512 * 1024 * 1024 and sha(raw) == receipt["sha256"],
            "independent archive changed/oversized",
        )
        return raw


class ContainerStatefulBackend:
    def __init__(self, binding, folder, store, *, command=None, operation_guard=None):
        self.binding, self.folder, self.store = binding, Path(folder), store
        self.command = command or self._command
        self.guard = operation_guard
        need(
            binding["node"] in {"postgresql", "mongodb"}
            and binding["generation"]
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", binding["database"]),
            "invalid database binding",
        )
        need(
            binding["source_id"] != binding["restore_id"]
            and all(
                re.fullmatch(r"[a-f0-9]{64}", binding[k])
                for k in ("source_id", "restore_id")
            ),
            "exact independent container IDs required",
        )
        need(
            binding.get("lab") is True or binding.get("accepted") is True,
            "unaccepted engine stop binding",
        )
        if not binding.get("lab"):
            need(
                self.guard is not None and self.guard.operation == "destroy",
                "common operation guard required",
            )
            need(
                re.fullmatch(r"i-[a-f0-9]{17}", binding.get("instance_id", "")),
                "accepted host identity required",
            )
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.control = self.folder / "engine-control.json"

    @staticmethod
    def _command(argv, *, input=None):
        value = subprocess.run(
            argv, input=input, capture_output=True, timeout=180, check=True
        )
        return value.stdout

    def _node(self, node):
        need(node == self.binding["node"], "database identity mismatch")

    def _run(self, container, *args, input=None, env=()):
        if self.guard:
            token = self.guard.check()
            need(
                self.guard.operation == "destroy"
                and token["scope"]["generation"] == self.binding["generation"],
                "wrong engine maintenance operation/generation",
            )
        need(
            container in (self.binding["source_id"], self.binding["restore_id"]),
            "unbound container",
        )
        actual = json.loads(self.command(["docker", "inspect", container]))
        need(
            len(actual) == 1
            and actual[0]["Id"] == container
            and actual[0]["State"]["Running"] is True,
            "container replaced/stopped",
        )
        return self.command(
            ["docker", "exec", "-i", *env, container, *args], input=input
        )

    def _pg(self, container, sql):
        admin = self.binding["admin_user"]
        need(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", admin), "invalid admin role")
        env = (
            ["--env", "PGPASSFILE=" + self.binding["pgpass_file"]]
            if self.binding.get("pgpass_file")
            else []
        )
        need(
            not env or self.binding["pgpass_file"].startswith("/run/mlo/secrets/"),
            "unsafe credential reference",
        )
        return self._run(
            container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            admin,
            "-d",
            self.binding["database"],
            "-Atc",
            sql,
            env=env,
        )

    def _mongo(self, container, script):
        database = json.dumps(self.binding["database"])
        if self.binding.get("lab"):
            prefix = "const target=db.getSiblingDB(" + database + ");"
            return self._run(container, "mongosh", "--quiet", "--eval", prefix + script)
        path = (
            self.binding["mongo_uri_file"]
            if container == self.binding["source_id"]
            else self.binding["restore_mongo_uri_file"]
        )
        need(
            path.startswith("/run/mlo/secrets/"),
            "protected MongoDB URI reference required",
        )
        prefix = (
            'const target=new Mongo(require("fs").readFileSync('
            + json.dumps(path)
            + ',"utf8").trim()).getDB('
            + database
            + ");"
        )
        return self._run(
            container, "mongosh", "--quiet", "--nodb", "--eval", prefix + script
        )

    def fingerprint(self, container):
        if self.binding["node"] == "postgresql":
            tables = self._pg(
                container,
                "select coalesce(json_agg(x order by schemaname,tablename),'[]') from (select schemaname,tablename from pg_tables where schemaname not like 'pg_%' and schemaname<>'information_schema') x",
            )
            values = []
            for item in json.loads(tables):
                quoted = (
                    '"'
                    + item["schemaname"].replace('"', '""')
                    + '"."'
                    + item["tablename"].replace('"', '""')
                    + '"'
                )
                rows = self._pg(
                    container,
                    "select coalesce(jsonb_agg(to_jsonb(t) order by to_jsonb(t)::text),'[]'::jsonb)::text from "
                    + quoted
                    + " t",
                )
                values.append([item, sha(rows.strip())])
            structure = self._pg(
                container,
                "select coalesce(json_agg(x order by table_schema,table_name,ordinal_position),'[]') from (select table_schema,table_name,ordinal_position,column_name,data_type,is_nullable,column_default from information_schema.columns where table_schema not like 'pg_%' and table_schema<>'information_schema') x",
            )
            sequences = self._pg(
                container,
                "select coalesce(json_agg(x order by schemaname,sequencename),'[]') from (select schemaname,sequencename,last_value from pg_sequences where schemaname not like 'pg_%') x",
            )
            indexes = self._pg(
                container,
                "select coalesce(json_agg(x order by schemaname,tablename,indexname),'[]') from (select schemaname,tablename,indexname,indexdef from pg_indexes where schemaname not like 'pg_%') x",
            )
            constraints = self._pg(
                container,
                "select coalesce(json_agg(x order by schema_name,table_name,name),'[]') from (select n.nspname as schema_name,c.relname as table_name,k.conname as name,pg_get_constraintdef(k.oid) as definition from pg_constraint k join pg_class c on c.oid=k.conrelid join pg_namespace n on n.oid=c.relnamespace where n.nspname not like 'pg_%' and n.nspname<>'information_schema') x",
            )
            return sha(
                json.dumps(
                    [
                        values,
                        sha(structure.strip()),
                        sha(sequences.strip()),
                        sha(indexes.strip()),
                        sha(constraints.strip()),
                    ],
                    sort_keys=True,
                ).encode()
            )
        raw = self._mongo(
            container,
            """const names=target.getCollectionNames().sort(); print(EJSON.stringify(names.map(n=>({name:n,docs:target.getCollection(n).find({}).sort({_id:1}).toArray(),indexes:target.getCollection(n).getIndexes().sort((a,b)=>a.name.localeCompare(b.name)),options:target.getCollectionInfos({name:n})[0].options}))));""",
        )
        return sha(raw.strip())

    def quiesce(self, node):
        self._node(node)
        source = self.binding["source_id"]
        need(not self.control.exists(), "existing stop attempt requires reconciliation")
        if node == "postgresql":
            roles = self.binding["writer_roles"]
            need(
                roles
                and self.binding["admin_user"] not in roles
                and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", r) for r in roles),
                "separate admin and complete application writer roles required",
            )
            for role in roles:
                self._pg(source, 'ALTER ROLE "' + role + '" NOLOGIN')
            names = ",".join("'" + r + "'" for r in roles)
            self._pg(
                source,
                "select pg_terminate_backend(pid) from pg_stat_activity where usename in ("
                + names
                + ") and pid<>pg_backend_pid()",
            )
            self._pg(
                source,
                'ALTER DATABASE "'
                + self.binding["database"]
                + '" SET default_transaction_read_only=on',
            )
        else:
            result = json.loads(
                self._mongo(
                    source,
                    "const r=target.adminCommand({fsync:1,lock:true});print(JSON.stringify({ok:Number(r.ok),lockCount:Number(r.lockCount)}))",
                )
            )
            need(
                result.get("ok") == 1 and result.get("lockCount", 0) >= 1,
                "MongoDB write lock not confirmed",
            )
        state = {
            "node": node,
            "generation": self.binding["generation"],
            "fingerprint": self.fingerprint(source),
            "writes_blocked": True,
            "barrier_id": self.binding["source_id"] + ":" + self.binding["generation"],
        }
        atomic_json(self.control, state)
        self._barrier()
        return {k: state[k] for k in ("node", "writes_blocked", "barrier_id")}

    def _barrier(self):
        state = json.loads(self.control.read_text())
        need(
            state["generation"] == self.binding["generation"]
            and state["node"] == self.binding["node"],
            "mixed stop generation",
        )
        if self.binding["node"] == "postgresql":
            need(
                self._pg(
                    self.binding["source_id"], "show default_transaction_read_only"
                ).strip()
                == b"on",
                "PostgreSQL write barrier lost",
            )
            for role in self.binding["writer_roles"]:
                need(
                    self._pg(
                        self.binding["source_id"],
                        "select rolcanlogin from pg_roles where rolname='" + role + "'",
                    ).strip()
                    == b"f",
                    "writer login re-enabled",
                )
        else:
            need(
                self._mongo(
                    self.binding["source_id"],
                    "print(target.adminCommand({currentOp:1}).fsyncLock===true)",
                ).strip()
                == b"true",
                "MongoDB lock lost",
            )
        need(
            self.fingerprint(self.binding["source_id"]) == state["fingerprint"],
            "source data changed after barrier",
        )
        return state

    def backup(self, node):
        self._node(node)
        state = self._barrier()
        source, name = self.binding["source_id"], self.binding["database"]
        if node == "postgresql":
            env = (
                ["--env", "PGPASSFILE=" + self.binding["pgpass_file"]]
                if self.binding.get("pgpass_file")
                else []
            )
            raw = self._run(
                source,
                "pg_dump",
                "-U",
                self.binding["admin_user"],
                "-d",
                name,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                env=env,
            )
        else:
            args = (
                ["--config=" + self.binding["mongo_tools_config"]]
                if not self.binding.get("lab")
                else []
            )
            raw = self._run(
                source, "mongodump", *args, "--db", name, "--archive", "--gzip"
            )
        need(raw and len(raw) <= 512 * 1024 * 1024, "archive empty/oversized")
        path = self.folder / "archive.dump"
        with path.open("xb") as stream:
            stream.write(raw)
        return {
            "location": str(path),
            "sha256": sha(raw),
            "verified": True,
            "barrier_id": state["barrier_id"],
        }

    def copy_backup(self, node, archive):
        self._node(node)
        raw = Path(archive["location"]).read_bytes()
        need(sha(raw) == archive["sha256"], "archive changed before independent copy")
        receipt = self.store.put(
            self.binding["generation"] + "-" + node + "-" + archive["sha256"] + ".dump",
            raw,
        )
        atomic_json(self.folder / "copy.json", receipt)
        return receipt

    def verify_copy(self, node, copied):
        self._node(node)
        return {
            "verified": sha(self.store.get(copied)) == copied["sha256"],
            "sha256": copied["sha256"],
        }

    def verify_restore(self, node, copied):
        self._node(node)
        self._barrier()
        raw = self.store.get(copied)
        need(
            sha((self.folder / "archive.dump").read_bytes()) == copied["sha256"],
            "source archive changed",
        )
        target, name = self.binding["restore_id"], self.binding["database"]
        if node == "postgresql":
            count = self._pg(
                target,
                "select count(*) from pg_tables where schemaname not like 'pg_%' and schemaname<>'information_schema'",
            ).strip()
            need(count == b"0", "restore destination is not empty")
            env = (
                ["--env", "PGPASSFILE=" + self.binding["pgpass_file"]]
                if self.binding.get("pgpass_file")
                else []
            )
            self._run(
                target,
                "pg_restore",
                "-U",
                self.binding["admin_user"],
                "-d",
                name,
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                input=raw,
                env=env,
            )
        else:
            need(
                self._mongo(target, "print(target.getCollectionNames().length)").strip()
                == b"0",
                "restore destination is not empty",
            )
            args = (
                ["--config=" + self.binding["restore_mongo_tools_config"]]
                if not self.binding.get("lab")
                else []
            )
            self._run(
                target,
                "mongorestore",
                *args,
                "--archive",
                "--gzip",
                "--nsInclude=" + name + ".*",
                input=raw
            )
        expected = json.loads(self.control.read_text())["fingerprint"]
        need(
            self.fingerprint(target) == expected,
            "engine restore content/schema differs",
        )
        atomic_json(
            self.folder / "restore.json",
            {"sha256": copied["sha256"], "fingerprint": expected, "verified": True},
        )
        return {"verified": True, "sha256": copied["sha256"]}

    def stop(self, node):
        self._node(node)
        if getattr(self, "shutdown_check", None):
            self.shutdown_check()
        self._barrier()
        copied = json.loads((self.folder / "copy.json").read_text())
        proof = json.loads((self.folder / "restore.json").read_text())
        need(
            proof["verified"] is True
            and proof["sha256"] == copied["sha256"]
            and sha(self.store.get(copied)) == copied["sha256"]
            and sha((self.folder / "archive.dump").read_bytes()) == copied["sha256"]
            and self.fingerprint(self.binding["restore_id"]) == proof["fingerprint"],
            "backup/restore drift before stop",
        )
        if self.guard:
            self.guard.check()
        self.command(["docker", "stop", "--time", "30", self.binding["source_id"]])

    def observe_stopped(self, node):
        self._node(node)
        value = json.loads(
            self.command(["docker", "inspect", self.binding["source_id"]])
        )
        return (
            len(value) == 1
            and value[0]["Id"] == self.binding["source_id"]
            and value[0]["State"]["Running"] is False
        )
