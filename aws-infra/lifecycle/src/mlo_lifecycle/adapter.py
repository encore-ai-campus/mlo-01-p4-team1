"""Local SQLite assets and B's real planned-stop protocol. No AWS adapter exists."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

from .journal import atomic
from .model import BASE, ROOT, digest, file_hash, need, read

sys.path.insert(0, str(ROOT / "aws-infra/runtime/src"))
from mlo_runtime.operations import stop_stateful
from mlo_runtime.stateful import CommandStatefulBackend


@contextmanager
def database(path, *, create=False):
    path = Path(path)
    need(not path.is_symlink() and (create or path.is_file()), "missing/unsafe local database")
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class SQLiteStatefulBackend:
    """A disposable SQLite service, never a claim about actual PG/Mongo behavior."""

    def __init__(self, lab, node, generation):
        self.folder = Path(lab) / ("db-" + node)
        self.node, self.generation = node, generation
        self.source = self.folder / "source.db"
        self.flags = self.folder / "control.json"

    def _flags(self):
        return read(self.flags)

    def quiesce(self, node):
        need(node == self.node, "stateful node mismatch")
        value = self._flags()
        value["writes_blocked"] = True
        atomic(self.flags, value)
        return {"node": node, "writes_blocked": True, "barrier_id": value["barrier_id"]}

    def backup(self, node):
        flags = self._flags()
        need(flags["writes_blocked"] is True and flags["stopped"] is False, "database not quiescent")
        archive = self.folder / "backup.db"
        need(not archive.exists(), "backup archive already exists; reconcile")
        with database(self.source) as source, database(archive, create=True) as copy:
            source.backup(copy)
        return {"sha256": file_hash(archive), "verified": True, "location": str(archive),
                "barrier_id": flags["barrier_id"]}

    def copy_backup(self, node, archive):
        source = self.folder / "backup.db"
        need(archive["location"] == str(source) and file_hash(source) == archive["sha256"], "backup mismatch")
        copy = self.folder / "independent" / "backup.db"
        copy.parent.mkdir(exist_ok=True)
        need(not copy.exists(), "independent archive already exists")
        with source.open("rb") as inp, copy.open("xb") as out:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        return {"location": str(copy), "sha256": file_hash(copy)}

    def verify_copy(self, node, copied):
        path = self.folder / "independent/backup.db"
        need(copied["location"] == str(path), "copy path mismatch")
        return {"verified": file_hash(path) == copied["sha256"], "sha256": file_hash(path)}

    def verify_restore(self, node, copied):
        checked = self.verify_copy(node, copied)
        need(checked["verified"] is True and checked["sha256"] == copied["sha256"],
             "copy changed before restore", code="backup_failure")
        need(file_hash(self.folder / "backup.db") == copied["sha256"], "archive changed before restore",
             code="backup_failure")
        restored = self.folder / "restored.db"
        need(not restored.exists(), "restore destination already exists")
        with database(copied["location"]) as source, database(restored, create=True) as target:
            source.backup(target)
        with database(self.source) as live, database(restored) as copy:
            original = live.execute("select id,payload from events order by id").fetchall()
            actual = copy.execute("select id,payload from events order by id").fetchall()
            need(actual == original and actual, "restored data differs")
            need(copy.execute("pragma integrity_check").fetchone() == ("ok",), "restore integrity failed")
        atomic(self.folder / "restore-proof.json", {"sha256": copied["sha256"], "rows": len(actual),
                                                     "content_sha256": digest(actual), "verified": True,
                                                     "restored_sha256": file_hash(restored)})
        return {"verified": True, "sha256": copied["sha256"]}

    def validate_backup_chain(self):
        proof = read(self.folder / "restore-proof.json")
        need(proof["verified"] is True, "restore not verified", code="backup_failure")
        for name in ("backup.db", "independent/backup.db"):
            need(file_hash(self.folder / name) == proof["sha256"], "backup archive/copy drifted",
                 code="backup_failure")
        need(file_hash(self.folder / "restored.db") == proof["restored_sha256"], "restored file drifted",
             code="backup_failure")
        for name in ("source.db", "restored.db"):
            with database(self.folder / name) as connection:
                rows = connection.execute("select id,payload from events order by id").fetchall()
                need(digest(rows) == proof["content_sha256"] and len(rows) == proof["rows"],
                     "live/restored content drifted", code="backup_failure")
                need(connection.execute("pragma integrity_check").fetchone() == ("ok",),
                     "database integrity failed", code="backup_failure")

    def stop(self, node):
        self.validate_backup_chain()
        flags = self._flags()
        need(flags["writes_blocked"] is True, "service restarted during shutdown")
        atomic(self.flags, {**flags, "stopped": True})

    def observe_stopped(self, node):
        return self._flags()["stopped"] is True


def initialize(lab, bundle):
    from .model import validate_bundle
    targets = validate_bundle(bundle)
    lab = Path(lab)
    need(not lab.exists(), "lab-init requires a new directory")
    lab.mkdir(parents=True, mode=0o700)
    atomic(lab / "identity.json", {"input_sha256": digest(bundle), "plan_sha256": None,
                                  "example_only": True, "generation": bundle["binding"]["generation"]})
    atomic(lab / "activity.json", {"creation": False, "deployment": False})
    (lab / "data").mkdir()
    for target, resource in targets.items():
        if resource["kind"] == "data":
            atomic(lab / "data" / (digest(target) + ".json"), {"target": target, "id": resource["id"],
                                                              "payload": "synthetic persistent data"})
    with database(lab / "simulator.db", create=True) as connection:
        connection.executescript("""
            create table assets(target text primary key, identity text not null, present integer not null);
            create table effects(step text primary key, proof text not null);
            create table runtime(key text primary key, value integer not null);
        """)
        connection.executemany("insert into assets values(?,?,1)", [(k, v["id"]) for k, v in targets.items()])
        connection.executemany("insert into runtime values(?,?)", [("frozen", 0), ("jobs", 3), ("restarts", 1)])
    for node in ("postgresql", "mongodb"):
        folder = lab / ("db-" + node)
        folder.mkdir()
        with database(folder / "source.db", create=True) as connection:
            connection.execute("create table events(id integer primary key, payload text not null)")
            connection.executemany("insert into events values(?,?)", [(n, "preserve-" + str(n)) for n in range(8)])
        atomic(folder / "control.json", {"writes_blocked": False, "stopped": False,
                                        "barrier_id": digest([bundle["binding"]["generation"], node])})
    if "track2" in bundle:
        from .track2 import initialize as initialize_track2
        identity = read(lab / "identity.json")
        atomic(lab / "identity.json", {**identity, "track2_sha256": digest(bundle["track2"])})
        atomic(lab / "track2-spec.json", bundle["track2"])
        initialize_track2(lab, bundle["track2"])


class Simulator:
    def __init__(self, lab, bundle):
        self.lab, self.bundle = Path(lab), bundle
        need(not self.lab.is_symlink(), "unsafe lab directory")
        need(not any(p.is_symlink() for p in self.lab.rglob("*")), "symlink inside local lab refused")
        need(read(self.lab / "identity.json")["input_sha256"] == digest(bundle), "lab target/state changed")

    def preflight(self, plan_hash, *, bind=False):
        identity = read(self.lab / "identity.json")
        if "track2" in self.bundle:
            need(identity.get("track2_sha256") == digest(self.bundle["track2"])
                 and read(self.lab / "track2-spec.json") == self.bundle["track2"], "Track2 hook registry changed")
        need(identity["plan_sha256"] in (None, plan_hash), "lab already bound to another plan; reconcile")
        need(read(self.lab / "activity.json") == {"creation": False, "deployment": False},
             "creation/deployment overlaps shutdown")
        with database(self.lab / "simulator.db") as connection:
            need(dict(connection.execute("select target,identity from assets")) == self.bundle["binding"]["targets"],
                 "observed address/ID drifted")
            need(all(type(row[0]) is int and row[0] in (0, 1) for row in connection.execute("select present from assets")),
                 "invalid resource presence observation")
        if bind and identity["plan_sha256"] is None:
            atomic(self.lab / "identity.json", {**identity, "plan_sha256": plan_hash})

    def _runtime(self, connection):
        return dict(connection.execute("select key,value from runtime").fetchall())

    def observe(self, step):
        with database(self.lab / "simulator.db") as connection:
            effect = connection.execute("select proof from effects where step=?", (step["id"],)).fetchone()
            if step["action"].startswith("track2-"):
                from .track2 import observe
                return observe(self.lab, self.bundle["track2"], step, json.loads(effect[0]) if effect else None)
            if not effect:
                runtime = self._runtime(connection)
                if step["action"] == "freeze" and (runtime["frozen"] or not runtime["restarts"]):
                    return {"ready_for_aws": False, "status": "unknown"}
                if step["action"] == "idle" and runtime["jobs"] == 0:
                    return {"ready_for_aws": False, "status": "unknown"}
                if step["action"] == "delete":
                    current = connection.execute("select present from assets where target=?", (step["target"],)).fetchone()
                    if current != (1,):
                        return {"ready_for_aws": False, "status": "unknown"}
                if step["action"] == "backup-data" and (self.lab / "data" / (digest(step["target"]) + ".copy.json")).exists():
                    return {"ready_for_aws": False, "status": "unknown"}
                # A stateful hook may have acted before the atomic simulator effect was written.
                if step["action"] == "stateful":
                    folder = self.lab / ("db-" + step["target"])
                    if read(folder / "control.json")["writes_blocked"] or (folder / "stop.receipt.json").exists():
                        return {"ready_for_aws": False, "status": "unknown"}
                return {"ready_for_aws": False, "status": "not_started"}
            proof = json.loads(effect[0])
            runtime = self._runtime(connection)
            need(runtime["frozen"] == 1 and runtime["restarts"] == 0, "service restart observed")
            if step["action"] != "freeze":
                need(runtime["jobs"] == 0, "cluster is no longer idle")
            if step["action"] == "delete":
                row = connection.execute("select present from assets where target=?", (step["target"],)).fetchone()
                need(row == (0,), "confirmed deletion drifted")
            if step["action"] == "stateful":
                folder = self.lab / ("db-" + step["target"])
                SQLiteStatefulBackend(self.lab, step["target"], self.bundle["binding"]["generation"]).validate_backup_chain()
                need(read(folder / "control.json")["stopped"] is True, "DB restart observed")
                need(file_hash(folder / "independent/backup.db") == proof["backup_sha256"], "backup evidence drifted")
                need(read(folder / "restore-proof.json")["sha256"] == proof["backup_sha256"], "restore evidence missing")
                need(digest(read(folder / "stop.receipt.json")) == proof["receipt_sha256"], "B receipt changed")
                expected = read(folder / "restore-proof.json")["content_sha256"]
                for db in (folder / "source.db", folder / "restored.db"):
                    with database(db) as source:
                        need(digest(source.execute("select id,payload from events order by id").fetchall()) == expected,
                             "DB changed after backup/restore")
            if step["action"] == "backup-data":
                copy = self.lab / "data" / (digest(step["target"]) + ".copy.json")
                source = self.lab / "data" / (digest(step["target"]) + ".json")
                need(file_hash(copy) == proof["backup_sha256"], "purge backup copy changed")
                need(file_hash(source) == proof["backup_sha256"], "purge source changed after backup")
                need(read(copy) == proof["restored"], "purge restore evidence mismatch")
            return {"ready_for_aws": False, "status": "completed", "proof": proof}

    def apply(self, step):
        need(self.observe(step)["status"] == "not_started", "duplicate/ambiguous adapter invocation")
        action, target = step["action"], step["target"]
        if "track2" in self.bundle and action in {"stateful", "delete", "backup-data"}:
            from .track2 import sealed
            sealed(self.lab, self.bundle["track2"])
        if action == "delete":
            for node in ("postgresql", "mongodb"):
                need(self.observe({"id": "stateful:" + node, "action": "stateful", "target": node})["status"] == "completed",
                     "current stateful evidence missing")
        if action.startswith("track2-"):
            from .track2 import apply
            proof = apply(self.lab, self.bundle["track2"], step)
        elif action == "stateful":
            hook = BASE / "scripts/stateful-hook.py"
            binding = {"node": target, "generation": self.bundle["binding"]["generation"],
                       "hook": str(hook), "sha256": file_hash(hook)}
            backend = CommandStatefulBackend(binding, self.lab / ("db-" + target))
            # The fixed hook receives only this process's validated local lab location.
            os.environ["MLO_C_LAB"] = str(self.lab)
            receipt = stop_stateful(target, backend, receipt_path=backend.folder / "stop.receipt.json")
            proof = {"backup_sha256": receipt["backup"]["sha256"],
                     "receipt_sha256": digest(receipt), "local_database": "SQLite",
                     "scope": "local protocol test; real PG/Mongo hooks not_run"}
        elif action == "backup-data":
            source = self.lab / "data" / (digest(target) + ".json")
            copy = source.with_suffix(".copy.json")
            need(not copy.exists(), "purge backup exists; reconcile")
            atomic(copy, read(source))
            need(file_hash(source) == file_hash(copy), "purge independent copy hash mismatch")
            proof = {"backup_sha256": file_hash(copy), "restored": read(copy)}
        else:
            proof = {"target": target, "action": action}
        with database(self.lab / "simulator.db") as connection:
            runtime = self._runtime(connection)
            if action == "freeze":
                connection.execute("update runtime set value=1 where key='frozen'")
                connection.execute("update runtime set value=0 where key='restarts'")
            else:
                need(runtime["frozen"] == 1 and runtime["restarts"] == 0, "new work/restart not inhibited")
                if action == "idle":
                    connection.execute("update runtime set value=0 where key='jobs'")
                elif action != "track2-freeze":
                    need(runtime["jobs"] == 0, "work/driver/executor not idle")
                if action == "delete":
                    if target.startswith(tuple(a + ":aws_ebs_volume." for a in self.bundle["inventory"]["accounts"])) or ":aws_s3_bucket.data" in target:
                        backup_step = {"id": "backup-data:" + target, "action": "backup-data", "target": target}
                        # Observe using another connection before deleting any synthetic data asset.
                        need(self.observe(backup_step)["status"] == "completed", "purge backup/restore evidence missing")
                    for node in ("postgresql", "mongodb"):
                        need(read(self.lab / ("db-" + node) / "control.json")["stopped"] is True,
                             "stateful stop not observed")
                    count = connection.execute("update assets set present=0 where target=? and present=1", (target,)).rowcount
                    need(count == 1, "delete target missing/already acted")
            connection.execute("insert into effects values(?,?)", (step["id"], json.dumps(proof)))
        return self.observe(step)

    def assets(self):
        with database(self.lab / "simulator.db") as connection:
            return dict(connection.execute("select target,present from assets").fetchall())
