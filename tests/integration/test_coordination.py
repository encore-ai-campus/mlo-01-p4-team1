"""Real SDK/HTTP/CAS and separate processes, plus actual A/B/C entry guards."""

import copy
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "aws-infra/contracts"),
    str(ROOT / "aws-infra/lifecycle/src"),
    str(ROOT / "aws-infra/lifecycle/tests"),
    str(ROOT / "aws-infra/foundation/scripts"),
    str(ROOT / "aws-infra/foundation/tests"),
    str(ROOT / "aws-infra/runtime/src"),
]
from infra_common.coordination import (
    OperationLease,
    S3CAS,
    MaintenanceBarrier,
    CoordinationError,
    scope_key,
)
from infra_common.factory import BootstrapGuard
from local_s3 import LocalS3
from support import fixture, authorization
from mlo_lifecycle.adapter import initialize, Simulator
from mlo_lifecycle.engine import execute
from mlo_lifecycle.planner import plan


def scope():
    b = fixture()["binding"]
    return {
        "project": "mlo-p4",
        "environment": "dev",
        "generation": b["generation"],
        "account_ids": b["account_ids"],
        "state_keys": {r: v["backend"]["key"] for r, v in b["state_bindings"].items()},
    }


class CoordinationIntegration(unittest.TestCase):
    def setUp(self):
        self.lab = LocalS3().__enter__()
        self.client = self.lab.client()
        self.scope = scope()
        self.key = "operations/" + scope_key(self.scope) + ".json"
        self.store = S3CAS(self.client, "lab-state", self.key)
        OperationLease(self.store, self.scope, "bootstrap", "create").initialize()

    def tearDown(self):
        self.client.close()
        self.lab.__exit__()

    def lease(self, owner, operation, **kwargs):
        return OperationLease(self.store, self.scope, owner, operation, **kwargs)

    def test_real_sdk_conditional_race_and_durable_epochs(self):
        first, etag = self.store.read()

        def put(owner):
            try:
                self.store.cas(etag, {**first, "owner": owner})
                return True
            except Exception:
                return False

        with concurrent.futures.ThreadPoolExecutor(2) as workers:
            results = list(workers.map(put, ["a", "b"]))
        self.assertEqual(sum(results), 1)
        with self.lease("a", "create").hold() as guard:
            epoch = guard.check()["epoch"]
            for action in ["deploy", "destroy"]:
                with self.assertRaises(CoordinationError):
                    self.lease("b", action).acquire()
        with self.lease("b", "destroy").hold() as guard:
            self.assertGreater(guard.check()["epoch"], epoch)
        self.assertEqual(self.store.read()[0]["state"], "complete")

    def test_expiry_process_death_and_generation_never_erase_uncertainty(self):
        guard = self.lease("dead", "destroy", clock=lambda: 100, ttl=1)
        guard.acquire()
        with self.assertRaises(CoordinationError):
            self.lease("other", "create", clock=lambda: 1000).acquire()
        changed = {**self.scope, "generation": "new-generation"}
        self.assertEqual(scope_key(changed), scope_key(self.scope))
        with self.assertRaises(CoordinationError):
            OperationLease(self.store, changed, "other", "deploy").acquire()
        guard.clock = lambda: 1000
        with self.assertRaises(CoordinationError):
            guard.check()

    def test_response_lost_after_write_never_retries_or_acquires_again(self):
        self.lab.drop_after_put = True
        with self.assertRaises(Exception):
            self.lease("lost-reply", "destroy").acquire()
        self.assertEqual(self.store.read()[0]["state"], "held")
        with self.assertRaises(CoordinationError):
            self.lease("retry", "destroy").acquire()

    def test_failed_renewal_self_fences_and_does_not_release(self):
        guard = self.lease("partitioned", "destroy", ttl=1)
        with self.assertRaises(CoordinationError):
            with guard.hold():
                self.lab.unavailable = True
                time.sleep(0.4)
                guard.check()
        self.lab.unavailable = False
        self.assertIn(self.store.read()[0]["state"], ["held", "ambiguous"])
        with self.assertRaises(CoordinationError):
            self.lease("b", "deploy").acquire()

    def test_separate_controller_processes_only_one_owner(self):
        source = """
import json,sys,time
from infra_common.coordination import S3CAS,OperationLease
import boto3
from botocore.config import Config
client=boto3.client('s3',endpoint_url=sys.argv[1],region_name='ap-northeast-2',aws_access_key_id='local',aws_secret_access_key='local',config=Config(proxies={},retries={'total_max_attempts':1},request_checksum_calculation='when_required',s3={'addressing_style':'path'}))
try:
 lease=OperationLease(S3CAS(client,'lab-state',sys.argv[2]),json.loads(sys.argv[3]),sys.argv[4],'destroy')
 lease.acquire(); print('owned',flush=True); time.sleep(2)
except Exception: print('blocked',flush=True)
finally: client.close()
"""
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        first = subprocess.Popen(
            [
                sys.executable,
                "-c",
                source,
                self.lab.endpoint,
                self.key,
                json.dumps(self.scope),
                "a",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            self.assertEqual(first.stdout.readline().strip(), "owned")
            other = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    source,
                    self.lab.endpoint,
                    self.key,
                    json.dumps(self.scope),
                    "b",
                ],
                capture_output=True,
                text=True,
                timeout=8,
                env=env,
            )
            self.assertEqual(other.stdout.strip(), "blocked", other.stderr)
            first.kill()
            first.communicate(timeout=5)
            with self.assertRaises(CoordinationError):
                self.lease(
                    "after-crash", "deploy", clock=lambda: time.time() + 120
                ).acquire()
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate(timeout=5)

    def test_actual_C_engine_uses_common_guard_and_blocks_other_parts(self):
        b = fixture()
        p = plan(b)
        key = b"local-authority-key-00000000000000"
        a, e = authorization(p, b, key)
        with tempfile.TemporaryDirectory() as tmp:
            lab = Path(tmp) / "lab"
            initialize(lab, b)
            with self.lease("C", "destroy").hold() as guard:
                with self.assertRaises(CoordinationError):
                    self.lease("A", "create").acquire()
                with self.assertRaises(CoordinationError):
                    self.lease("B", "deploy").acquire()
                self.assertTrue(
                    execute(p, b, lab, a, e, key, operation_guard=guard)["complete"]
                )
            self.assertEqual(
                sum(v == 0 for v in Simulator(lab, b).assets().values()), 55
            )

    def test_supervisor_maintenance_barrier_fences_running_group(self):
        from mlo_runtime.supervisor import Supervisor
        from mlo_runtime.operations import UnsafeTransition

        class Group:
            state = Path("/nonexistent")

            def _fence(self, token):
                self.fenced = token

        group = Group()
        supervisor = Supervisor(
            group, maintenance_barrier=MaintenanceBarrier(self.store, self.scope)
        )
        supervisor.token = {"owner": "control-a"}
        with self.lease("C", "destroy").hold():
            with self.assertRaises(UnsafeTransition):
                supervisor.step()
            self.assertEqual(group.fenced, {"owner": "control-a"})
            self.assertIsNone(supervisor.token)

    def test_bootstrap_controller_pending_receipt_blocks_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = BootstrapGuard(Path(tmp), self.scope, time.time() + 60)
            with self.assertRaises(RuntimeError):
                with first.hold():
                    raise RuntimeError("lost bootstrap reply")
            with self.assertRaises(CoordinationError):
                with BootstrapGuard(Path(tmp), self.scope, time.time() + 60).hold():
                    pass

    def test_actual_foundation_wrapper_checks_guard_before_fake_apply(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "wrapper_fixture", ROOT / "aws-infra/foundation/tests/test_wrapper.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        case = module.WrapperTests("test_real_gate_prevents_even_identity_call")
        case.setUp()
        try:
            case.run_stage("preflight")
            case.run_stage("bootstrap-plan")
            before = len(case.calls())
            import run_foundation

            with self.lease("C", "destroy").hold() as wrong:
                with self.assertRaises(ValueError):
                    run_foundation.execute(
                        "bootstrap",
                        case.binding,
                        case.root / "run",
                        enabled=True,
                        terraform=str(case.cli),
                        aws=str(case.cli),
                        operation_guard=wrong,
                    )
                self.assertEqual(len(case.calls()), before)
            actual_scope = {
                **self.scope,
                "generation": case.b["generation"],
                "account_ids": case.b["account_ids"],
            }
            with OperationLease(
                self.store, actual_scope, "A", "create"
            ).hold() as guard:
                run_foundation.execute(
                    "bootstrap",
                    case.binding,
                    case.root / "run",
                    enabled=True,
                    terraform=str(case.cli),
                    aws=str(case.cli),
                    operation_guard=guard,
                )
            self.assertTrue(any(args[0] == "apply" for args in case.calls()))
        finally:
            case.tearDown()

    def test_actual_runtime_archive_install_is_guarded_and_packages_common_code(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "install_fixture", ROOT / "aws-infra/runtime/tests/unit/test_install.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from mlo_runtime.install import execute as install

        with tempfile.TemporaryDirectory(prefix="mlo-common-installer-") as tmp:
            base = Path(tmp).resolve()
            host = base / "mlo-lab-root"
            payload, result = module.make_payload(base)
            binding = module.make_binding(host, result)
            backend = module.Backend()
            actual_scope = {**self.scope, "generation": "g1"}
            lease = OperationLease(self.store, actual_scope, "B", "deploy")
            with self.assertRaises(CoordinationError):
                install(
                    payload,
                    binding,
                    "install",
                    "dev",
                    "g1",
                    "advertising",
                    backend=backend,
                    operation_guard=lease,
                )
            self.assertFalse((host / "opt/mlo").exists())
            with lease.hold():
                receipt = install(
                    payload,
                    binding,
                    "install",
                    "dev",
                    "g1",
                    "advertising",
                    backend=backend,
                    operation_guard=lease,
                )
                self.assertEqual(receipt["status"], "success")
                self.assertTrue(
                    (
                        host
                        / "opt/mlo/current/aws-infra/contracts/infra_common/factory.py"
                    ).is_file()
                )
                with self.assertRaises(CoordinationError):
                    self.lease("C", "destroy").acquire()
            self.assertEqual(self.store.read()[0]["state"], "complete")

    def test_private_factory_uses_exact_scope_and_does_not_initialize_missing_record(
        self,
    ):
        from infra_common.factory import load_guard

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / ".private"
            folder.mkdir()
            path = folder / "coordination.json"
            value = {
                "schema_version": "infra-coordination-binding-v1",
                "accepted": True,
                "generation": self.scope["generation"],
                "scope_key": scope_key(self.scope),
                "backend": "s3-cas",
                "bucket": "lab-state",
                "key": self.key,
            }
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            guard = load_guard(path, self.scope, "A", "create", client=self.client)
            with guard.hold():
                self.assertEqual(guard.check()["operation"], "create")
            path.chmod(0o644)
            with self.assertRaises(CoordinationError):
                load_guard(path, self.scope, "B", "deploy", client=self.client)
        missing = S3CAS(self.client, "lab-state", "operations/missing-test.json")
        with self.assertRaises(CoordinationError):
            OperationLease(missing, self.scope, "A", "create").acquire()
        self.assertEqual(missing.read(), (None, None))
