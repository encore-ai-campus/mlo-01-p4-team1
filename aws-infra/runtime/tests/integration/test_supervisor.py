"""Real PG CAS, two local supervisors/process groups, dump/copy/restore stop gate.

Fixed-argv remote-host adapter is exercised with local processes. The enumerated
Spark observations are a controlled fixture, not real SSM/3AZ Spark acceptance.
"""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import psycopg

from mlo_runtime.execution import FixedCommandExecution
from mlo_runtime.lab import Lab
from mlo_runtime.lease import PostgresLease
from mlo_runtime.operations import (
    ActiveGroup,
    UnsafeTransition,
    atomic_json,
    backup,
    stop_stateful,
)
from mlo_runtime.supervisor import Supervisor

HOOK = r"""
import argparse,fcntl,json,os,signal,subprocess,sys,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--operation',choices=['start','stop','observe'],required=True)
p.add_argument('--owner',required=True);p.add_argument('--generation',required=True);p.add_argument('--epoch',type=int,required=True)
a=p.parse_args(); folder=Path(__file__).parent; path=folder/(a.owner+'.jobs.json')
def running(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')
with (folder/(a.owner+'.lock')).open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 value=json.loads(path.read_text()) if path.exists() else {'owner':a.owner,'generation':a.generation,'epoch':a.epoch,'pids':[]}
 live=[pid for pid in value['pids'] if running(pid)]
 if live and (value['owner'],value['generation'],value['epoch']) != (a.owner,a.generation,a.epoch): sys.exit(2)
 if (folder/(a.owner+'.unreachable')).exists(): sys.exit(3)
 if a.operation=='start' and not live:
  value={'owner':a.owner,'generation':a.generation,'epoch':a.epoch,'pids':[]}
  for role in ('weather-stream','feedback-postgresql','batch-submitter'):
   child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(3600)',role],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
   value['pids'].append(child.pid)
  path.write_text(json.dumps(value));live=value['pids']
 if a.operation=='stop':
  if (folder/(a.owner+'.stop-fail')).exists(): sys.exit(4)
  for pid in live:
   try: os.killpg(pid,signal.SIGTERM)
   except ProcessLookupError: pass
  end=time.monotonic()+3
  while any(running(pid) for pid in live) and time.monotonic()<end: time.sleep(.02)
  live=[pid for pid in value['pids'] if running(pid)]
 masters=['master-1','master-2','master-3'];workers=['worker-1','worker-2','worker-3']
 if (folder/'missing-master').exists(): masters.pop()
 if (folder/'missing-worker').exists(): workers.pop()
 idle=not live and not (folder/'executor-remains').exists()
 receipt={'accepted':True,'owner':a.owner,'generation':a.generation,'epoch':a.epoch,'operation':a.operation,'running':len(live)==3,'masters':masters,'workers':workers}
 receipt['evidence']={'owner':a.owner,'generation':a.generation,'epoch':a.epoch,'observed_at':time.time(),'containers_stopped':not live,'drivers_absent':not live,'executors_absent':idle,'cluster_idle':idle,'all_masters_observed':len(masters)==3,'all_workers_observed':len(workers)==3}
 print(json.dumps(receipt))
"""


WORKER = r"""
import json,signal,sys,threading
from pathlib import Path
from mlo_runtime.lease import PostgresLease
from mlo_runtime.execution import FixedCommandExecution
from mlo_runtime.operations import ActiveGroup
from mlo_runtime.supervisor import Supervisor
c=json.loads(Path(sys.argv[1]).read_text())
group=ActiveGroup(c['state'],'generation-1',c['owner'],PostgresLease(c['dsn'],c['group'],lab=True),FixedCommandExecution(c['execution']))
stopped=threading.Event()
signal.signal(signal.SIGTERM,lambda *_:stopped.set())
Supervisor(group,interval=.2).run(stopped)
"""


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("local supervisor/process condition timed out")


class SupervisorPostgresIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab("ops-pg")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.lab.configure(
            {
                "image": cls.lab.image("postgresql"),
                "mem_limit": "512m",
                "ports": [f"127.0.0.1:{cls.port}:5432"],
                "environment": {"POSTGRES_PASSWORD": "disposable-local-fixture"},
                "volumes": ["data:/var/lib/postgresql/data"],
            }
        )
        try:
            cls.lab.up()
            cls.lab.ready(["pg_isready", "-U", "postgres"])
            cls.dsn = f"host=127.0.0.1 port={cls.port} user=postgres password=disposable-local-fixture dbname=postgres sslmode=disable"
            cls.lease = PostgresLease(cls.dsn, lab=True)
            cls.lease.initialize()
        except Exception:
            cls.lab.close()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.processes = []
        self.addCleanup(self.cleanup_workers)
        self.group_name = "local-" + uuid.uuid4().hex
        self.lease = PostgresLease(self.dsn, self.group_name, lab=True)
        hook = self.folder / "host-adapter.py"
        hook.write_text("#!" + sys.executable + "\n" + HOOK)
        hook.chmod(0o700)
        self.binding = {
            "masters": ["master-1", "master-2", "master-3"],
            "workers": ["worker-1", "worker-2", "worker-3"],
            "owners": {
                owner: {
                    "target": owner,
                    "path": str(hook),
                    "sha256": hashlib.sha256(hook.read_bytes()).hexdigest(),
                }
                for owner in ("control-a", "control-b")
            },
        }

    def cleanup_workers(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for owner in ("control-a", "control-b"):
            path = self.folder / (owner + ".jobs.json")
            if path.exists():
                for pid in json.loads(path.read_text())["pids"]:
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def start_worker(self, owner):
        config = self.folder / (owner + ".json")
        atomic_json(
            config,
            {
                "state": str(self.folder / (owner + ".state.json")),
                "owner": owner,
                "dsn": self.dsn,
                "group": self.group_name,
                "execution": self.binding,
            },
        )
        script = self.folder / "worker.py"
        script.write_text(WORKER)
        stream = (self.folder / (owner + ".log")).open("ab")
        self.addCleanup(stream.close)
        process = subprocess.Popen(
            [sys.executable, str(script), str(config)], stdout=stream, stderr=stream
        )
        self.processes.append(process)
        return process

    def test_two_real_workers_wait_then_fence_killed_owner_before_takeover(self):
        a = self.start_worker("control-a")
        wait_for(lambda: (self.folder / "control-a.jobs.json").exists())
        initial = self.lease.read()
        pids = json.loads((self.folder / "control-a.jobs.json").read_text())["pids"]
        b = self.start_worker("control-b")
        time.sleep(0.5)
        self.assertFalse((self.folder / "control-b.jobs.json").exists())
        self.assertIsNone(a.poll())
        self.assertIsNone(b.poll())
        # Kill only A's supervisor; its three real work processes stay alive.
        a.kill()
        a.wait(timeout=3)
        wait_for(lambda: (self.folder / "control-b.jobs.json").exists(), timeout=22)
        token = self.lease.read()
        self.assertEqual(
            (token["owner"], token["epoch"]), ("control-b", initial["epoch"] + 1)
        )
        for pid in pids:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
            )
            self.assertTrue(
                result.returncode != 0
                or not result.stdout.strip()
                or result.stdout.strip().startswith("Z")
            )
        b.terminate()
        b.wait(timeout=8)
        self.assertEqual(
            json.loads((self.folder / "control-b.state.json").read_text())["phase"],
            "fenced",
        )

    def test_actual_pg_compare_and_swap_race_and_stale_renewal(self):
        barrier = threading.Barrier(2)
        now = self.lease.now()

        def compete(owner):
            lease = PostgresLease(self.dsn, self.group_name, lab=True)
            barrier.wait()
            return lease.cas(
                None,
                {
                    "owner": owner,
                    "generation": "generation-1",
                    "epoch": 1,
                    "expires_at": now + 15,
                },
            )

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(compete, ("control-a", "control-b")))
        self.assertEqual(sorted(results), [False, True])
        old = self.lease.read()
        self.assertFalse(self.lease.cas(None, {**old, "owner": "control-c"}))
        self.assertTrue(
            self.lease.cas(old, {**old, "expires_at": self.lease.now() + 16})
        )
        self.assertFalse(
            self.lease.cas(old, {**old, "expires_at": self.lease.now() + 17})
        )

    def test_live_restart_reuses_jobs_and_real_db_connection_failure_self_fences(self):
        execution = FixedCommandExecution(self.binding)
        group = ActiveGroup(
            self.folder / "a.state.json",
            "generation-1",
            "control-a",
            self.lease,
            execution,
        )
        first = Supervisor(group)
        first.step()
        self.assertEqual(first.health(), {"ready": True, "role": "active"})
        standby = Supervisor(
            ActiveGroup(
                self.folder / "b.state.json",
                "generation-1",
                "control-b",
                self.lease,
                execution,
            )
        )
        self.assertEqual(standby.health(), {"ready": True, "role": "standby"})
        pids = json.loads((self.folder / "control-a.jobs.json").read_text())["pids"]
        restarted = Supervisor(group)
        restarted.step()
        self.assertEqual(
            json.loads((self.folder / "control-a.jobs.json").read_text())["pids"], pids
        )
        # Connect to an actually closed loopback endpoint: no mock return value.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        self.lease.dsn = f"host=127.0.0.1 port={closed_port} user=postgres dbname=postgres sslmode=disable"
        with self.assertRaises(UnsafeTransition):
            restarted.step()
        self.assertEqual(json.loads(group.state.read_text())["phase"], "fenced")
        proof = execution.observe(first.token)
        self.assertTrue(
            proof.containers_stopped and proof.drivers_absent and proof.executors_absent
        )

    def test_remote_host_failure_remaining_executor_and_missing_observer_block(self):
        token = {
            "owner": "control-a",
            "generation": "generation-1",
            "epoch": 1,
            "expires_at": self.lease.now() + 15,
        }
        self.assertTrue(self.lease.cas(None, token))
        execution = FixedCommandExecution(self.binding)
        execution.ensure_started(token)
        # Expiry fixture changes only this dedicated group's row, not another run.
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                "UPDATE mlo_execution_lease_v1 SET expires_at=%s WHERE group_name=%s",
                (self.lease.now() - 1, self.group_name),
            )
        old = self.lease.read()
        for marker in (
            "control-a.stop-fail",
            "control-a.unreachable",
            "executor-remains",
            "missing-master",
            "missing-worker",
        ):
            path = self.folder / marker
            path.touch()
            group = ActiveGroup(
                self.folder / "b.state.json",
                "generation-1",
                "control-b",
                self.lease,
                execution,
            )
            with self.subTest(marker=marker), self.assertRaises(
                (UnsafeTransition, subprocess.SubprocessError)
            ):
                group.acquire()
            self.assertEqual(self.lease.read(), old)
            self.assertFalse((self.folder / "control-b.jobs.json").exists())
            path.unlink()

    def test_real_pg_dump_independent_copy_restore_then_planned_stop(self):
        # Dedicated databases inside this run's dedicated container.
        source, restored = "source_" + uuid.uuid4().hex, "restore_" + uuid.uuid4().hex
        self.lab.exec("createdb", "-U", "postgres", source)
        self.lab.exec("createdb", "-U", "postgres", restored)
        self.lab.exec(
            "psql",
            "-U",
            "postgres",
            "-d",
            source,
            "-c",
            "CREATE TABLE evidence(id integer PRIMARY KEY,value text);INSERT INTO evidence VALUES(1,'preserve-me')",
        )
        archive = self.folder / "local.dump"
        independent = self.folder / "independent" / "copy.dump"
        independent.parent.mkdir()
        lab, events = self.lab, []

        class Backend:
            def quiesce(inner, node):
                events.append("quiesce")
                lab.exec(
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    source,
                    "-c",
                    "ALTER DATABASE "
                    + source
                    + " SET default_transaction_read_only=on",
                )
                return {"node": node, "barrier_id": source, "writes_blocked": True}

            def backup(inner, node):
                events.append("backup")
                command = [
                    "docker",
                    "compose",
                    "--project-name",
                    lab.project,
                    "--file",
                    str(lab.compose_path),
                    "exec",
                    "-T",
                    lab.service,
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-Fc",
                    source,
                ]
                backup(
                    command,
                    archive,
                    lambda path: lab.exec(
                        "pg_restore", "--list", input=path.read_bytes()
                    ).returncode
                    == 0,
                )
                return {
                    "verified": True,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "location": str(archive),
                    "barrier_id": source,
                }

            def copy_backup(inner, node, value):
                events.append("copy")
                independent.write_bytes(archive.read_bytes())
                return {"location": str(independent), "sha256": value["sha256"]}

            def verify_copy(inner, node, value):
                events.append("verify-copy")
                return {
                    "verified": independent.read_bytes() == archive.read_bytes(),
                    "sha256": hashlib.sha256(independent.read_bytes()).hexdigest(),
                }

            def verify_restore(inner, node, value):
                events.append("verify-restore")
                lab.exec(
                    "pg_restore",
                    "-U",
                    "postgres",
                    "-d",
                    restored,
                    input=independent.read_bytes(),
                )
                result = lab.exec(
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    restored,
                    "-Atc",
                    "SELECT value FROM evidence WHERE id=1",
                )
                return {
                    "verified": result.stdout.strip() == b"preserve-me",
                    "sha256": hashlib.sha256(independent.read_bytes()).hexdigest(),
                }

            def stop(inner, node):
                events.append("stop")
                lab.compose("stop", lab.service)

            def observe_stopped(inner, node):
                events.append("observe-stopped")
                return not lab.compose("ps", "--status", "running", "-q").stdout.strip()

        try:
            receipt = stop_stateful(
                "postgresql", Backend(), receipt_path=self.folder / "stop.json"
            )
            self.assertEqual(receipt["phase"], "complete")
            self.assertEqual(
                events,
                [
                    "quiesce",
                    "backup",
                    "copy",
                    "verify-copy",
                    "verify-restore",
                    "stop",
                    "observe-stopped",
                ],
            )
        finally:
            lab.compose("start", lab.service)
            lab.ready(["pg_isready", "-U", "postgres"])
        observed = self.lab.exec(
            "psql",
            "-U",
            "postgres",
            "-d",
            source,
            "-Atc",
            "SELECT value FROM evidence WHERE id=1",
        )
        self.assertEqual(observed.stdout.strip(), b"preserve-me")


if __name__ == "__main__":
    unittest.main()
