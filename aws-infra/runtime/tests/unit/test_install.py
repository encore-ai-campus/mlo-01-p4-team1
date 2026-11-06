import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from mlo_runtime.install import InstallError, execute, deployment_lock
from mlo_runtime.packager import package
from mlo_runtime.artifact import inspect_artifact


def make_payload(base, version="v1", node="advertising", role="advertising", services=None, required_secrets=None):
    bundle = base / ("bundle-" + version)
    host = bundle / "hosts" / node
    host.mkdir(parents=True)
    (host / "compose.yml").write_text(json.dumps({"services": services or {}}))
    (host / "details.json").write_text(json.dumps({"platform": "linux/amd64"}))
    result = package(bundle, base / (version + ".tar.gz"), artifact_version=version,
                     environment="dev", generation="g1", node_roles={node: role},
                     image_digests={"fixture": next(iter(services.values()))["image"]} if services else {},
                     required_secrets={node: required_secrets or []}, fixture=True)
    payload = base / ("payload-" + version)
    inspect_artifact(result["archive"], result["sha256"], "dev", "g1", node,
                     payload, result["manifest_sha256"])
    return payload, result


def make_binding(root, *results, node="advertising", role="advertising"):
    root.mkdir(exist_ok=True)
    path = root / "host-binding.json"
    value = {"schema_version": "1.0.0", "instance_id": "i-0123456789abcdef0", "node": node,
             "role": role, "environment": "dev", "generation": "g1", "lab": True,
             "root": str(root), "image_digests": {}, "required_secrets": results[-1]["manifest"]["required_secrets"][node],
             "accepted_manifests": [r["manifest_sha256"] for r in results]}
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


class Backend:
    def __init__(self):
        self.events = []
        self.fail = set()
    def check_mount(self, path):
        self.events.append(("mount", str(path)))
    def start(self, release):
        self.events.append(("start", release.name))
    def health(self, release):
        self.events.append(("health", release.name))
        if release.name in self.fail:
            raise InstallError("readiness failed")
    def units(self, release):
        self.events.append(("units", release.name))
    def stop(self, release):
        self.events.append(("stop", release.name))


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mlo-install-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "mlo-lab-root"
        self.p1, self.r1 = make_payload(self.base)
        self.binding = make_binding(self.root, self.r1)
        self.backend = Backend()

    def run_operation(self, payload=None, operation="install", generation="g1", node="advertising"):
        return execute(payload or self.p1, self.binding, operation, "dev", generation,
                       node, backend=self.backend)

    def test_install_reapply_health_deploy_rollback(self):
        first = self.run_operation()
        self.assertTrue(first["changed"])
        self.assertFalse(self.run_operation()["changed"])
        self.assertFalse(self.run_operation(operation="health")["changed"])
        p2, r2 = make_payload(self.base, "v2")
        self.binding = make_binding(self.root, self.r1, r2)
        self.run_operation(p2, "deploy")
        root = self.root / "opt/mlo"
        self.assertEqual((root / "current").resolve().name, "v2")
        self.run_operation(p2, "rollback")
        self.assertEqual((root / "current").resolve().name, "v1")
        self.assertTrue((root / "releases/v2/release.json").exists())
        self.assertEqual(len(list((root / "receipts").glob("*.json"))), 5)

    def test_failed_deploy_preserves_previous_revision_and_data(self):
        self.run_operation()
        keep = self.root / "srv/mlo/g1/user-data"
        keep.parent.mkdir(parents=True)
        keep.write_text("keep")
        p2, r2 = make_payload(self.base, "v2")
        self.binding = make_binding(self.root, self.r1, r2)
        self.backend.fail.add("v2")
        with self.assertRaises(InstallError):
            self.run_operation(p2, "deploy")
        self.assertEqual((self.root / "opt/mlo/current").resolve().name, "v1")
        self.assertEqual(keep.read_text(), "keep")
        self.assertEqual(json.loads((self.root / "opt/mlo/state.json").read_text())["current"], "v1")
        self.assertTrue(any(json.loads(p.read_text())["status"] == "failed" for p in (self.root / "opt/mlo/receipts").glob("*.json")))

    def test_target_secret_manifest_and_role_fail_closed(self):
        for field, value in [("role", "control"), ("required_secrets", ["missing"]),
                             ("accepted_manifests", ["0" * 64])]:
            original = json.loads(self.binding.read_text())
            self.binding.write_text(json.dumps({**original, field: value}))
            with self.subTest(field=field), self.assertRaises(InstallError):
                self.run_operation()
            self.binding.write_text(json.dumps(original))
        with self.assertRaises(InstallError):
            self.run_operation(generation="other")
        with self.assertRaises(InstallError):
            self.run_operation(node="control-a")
        self.assertFalse((self.root / "opt/mlo/current").exists())

    def test_immutable_payload_and_binding_permissions(self):
        self.binding.chmod(0o644)
        with self.assertRaises(InstallError):
            self.run_operation()
        self.binding.chmod(0o600)
        (self.p1 / "bin/runtime-entrypoint.py").write_text("tampered")
        with self.assertRaises(InstallError):
            self.run_operation()

    def test_concurrent_deploy_blocked(self):
        lock = self.root / "opt/mlo/deploy.lock"
        with deployment_lock(lock), self.assertRaises(InstallError):
            self.run_operation()

    def test_fixture_not_allowed_for_production_binding(self):
        binding = json.loads(self.binding.read_text())
        binding["lab"] = False
        self.binding.write_text(json.dumps(binding))
        with self.assertRaises(InstallError):
            self.run_operation()

    def test_actual_missing_secret_rejected_before_backend_start(self):
        p2, r2 = make_payload(self.base, "v2", required_secrets=["/run/mlo/secrets/token"])
        self.binding = make_binding(self.root, r2)
        with self.assertRaises(InstallError):
            self.run_operation(p2)
        self.assertFalse(any(e[0] == "start" for e in self.backend.events))
        secret = self.root / "run/mlo/secrets/token"
        secret.parent.mkdir(parents=True)
        secret.write_text("ephemeral fixture")
        secret.chmod(0o600)
        self.assertEqual(self.run_operation(p2)["status"], "success")

    def test_first_install_failure_stops_partial_candidate_and_keeps_no_current(self):
        self.backend.fail.add("v1")
        with self.assertRaises(InstallError):
            self.run_operation()
        self.assertIn(("stop", "v1"), self.backend.events)
        self.assertFalse((self.root / "opt/mlo/current").exists())

    def test_stateful_rollback_requires_current_verified_remote_backup(self):
        with tempfile.TemporaryDirectory(prefix="mlo-stateful-install-") as temporary:
            base = Path(temporary).resolve()
            root = base / "mlo-stateful-lab"
            p1, r1 = make_payload(base, "v1", node="postgresql", role="postgresql")
            p2, r2 = make_payload(base, "v2", node="postgresql", role="postgresql")
            binding = make_binding(root, r1, r2, node="postgresql", role="postgresql")
            backend = Backend()
            def run(payload, operation):
                return execute(payload, binding, operation, "dev", "g1", "postgresql", backend=backend)
            run(p1, "install")
            accepted = json.loads(binding.read_text())
            evidence = {"backup_verified": True, "remote_copy_verified": True, "generation": "g1", "expires_at": time.time() + 60}
            binding.write_text(json.dumps({**accepted, "stateful_change": evidence}))
            run(p2, "deploy")
            for invalid in [{}, {**evidence, "generation": "other"}, {**evidence, "expires_at": 0}, {**evidence, "remote_copy_verified": False}]:
                binding.write_text(json.dumps({**accepted, "stateful_change": invalid}))
                before = list(backend.events)
                with self.assertRaises(InstallError):
                    run(p2, "rollback")
                self.assertEqual((root / "opt/mlo/current").resolve().name, "v2")
                self.assertFalse(any(e[0] == "start" for e in backend.events[len(before):]))
            binding.write_text(json.dumps({**accepted, "stateful_change": evidence}))
            self.assertTrue(run(p2, "rollback")["changed"])
            self.assertEqual((root / "opt/mlo/current").resolve().name, "v1")
