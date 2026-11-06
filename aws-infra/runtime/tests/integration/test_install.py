"""Actual stdlib entrypoint inside disposable Linux; no systemd/SSM claim."""
import json
import os
from pathlib import Path
import sys
import importlib.util
import unittest
import tempfile
import uuid

from mlo_runtime.contracts import RUNTIME
from mlo_runtime.lab import Lab
from mlo_runtime.install import ComposeSystemdBackend, InstallError, execute

spec = importlib.util.spec_from_file_location("install_unit_fixtures", RUNTIME / "tests/unit/test_install.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
make_payload = fixtures.make_payload
make_binding = fixtures.make_binding


class InstallerLinuxIntegration(unittest.TestCase):
    def test_real_compose_install_readiness_failure_and_rollback(self):
        """Real Linux containers; controller mount probe is explicitly a lab substitute."""
        with tempfile.TemporaryDirectory(prefix="mlo-compose-install-") as temporary:
            base = Path(temporary).resolve()
            root = base / "mlo-compose-lab"
            node = "install-" + uuid.uuid4().hex[:8]
            item = json.loads((RUNTIME / "images.lock.json").read_text())["images"]["haproxy"]
            image = item["ref"] + "@" + item["digest"]
            service = {"image": image, "platform": os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64"), "command": ["sleep", "infinity"],
                       "healthcheck": {"test": ["CMD", "/bin/sh", "-c", "kill -0 1"], "interval": "1s", "timeout": "1s", "retries": 2}, "mem_limit": "128m"}
            p1, r1 = make_payload(base, "v1", node=node, services={"fixture": service})
            p2, r2 = make_payload(base, "v2", node=node, services={"fixture": {**service, "command": ["haproxy", "-v"]}})
            binding = make_binding(root, r1, r2, node=node)
            value = json.loads(binding.read_text())
            value["image_digests"] = {"fixture": image}
            binding.write_text(json.dumps(value))
            class FixtureBackend(ComposeSystemdBackend):
                def check_mount(self, path):
                    path.mkdir(parents=True, exist_ok=True)
            backend = FixtureBackend(value)
            first_release = root / "opt/mlo/releases/v1"
            try:
                result = execute(p1, binding, "install", "dev", "g1", node, backend=backend)
                self.assertTrue(result["changed"])
                self.assertFalse(execute(p1, binding, "install", "dev", "g1", node, backend=backend)["changed"])
                with self.assertRaises(InstallError):
                    execute(p2, binding, "deploy", "dev", "g1", node, backend=backend)
                self.assertEqual((root / "opt/mlo/current").resolve().name, "v1")
                backend.health(first_release)
            finally:
                if (first_release / "installed-compose.json").exists():
                    backend.compose(first_release, "down", "--volumes", "--remove-orphans")

    def test_entrypoint_install_reapply_health_deploy_rollback_on_linux(self):
        lab = Lab("installer")
        self.addCleanup(lab.close)
        p1, r1 = make_payload(lab.folder, "v1")
        p2, r2 = make_payload(lab.folder, "v2")
        root = "/tmp/mlo-installer-lab"
        binding = {"schema_version": "1.0.0", "instance_id": "i-0123456789abcdef0", "node": "advertising",
                   "role": "advertising", "environment": "dev", "generation": "g1", "lab": True,
                   "root": root, "mount_target": root + "/srv/mlo", "image_digests": {},
                   "required_secrets": [], "accepted_manifests": [r1["manifest_sha256"], r2["manifest_sha256"]]}
        (lab.folder / "binding.json").write_text(json.dumps(binding))
        lab.configure({"image": "mlo-p4-b-host-lab:20261029", "volumes": ["data:" + root + "/srv/mlo", "data:/srv/mlo", f"{lab.folder}:/seed:ro", f"{RUNTIME}:/workspace:ro"], "command": ["sleep", "infinity"]})
        lab.up()
        lab.exec("/bin/sh", "-ec", "cp /seed/binding.json " + root + "/binding.json; chmod 600 " + root + "/binding.json")
        def command(version, operation):
            return lab.exec("/usr/bin/python3", f"/seed/payload-{version}/bin/runtime-entrypoint.py",
                            "--operation", operation, "--environment", "dev", "--generation", "g1",
                            "--node", "advertising", "--binding", root + "/binding.json")
        first = json.loads(command("v1", "install").stdout)
        self.assertTrue(first["changed"])
        self.assertFalse(json.loads(command("v1", "install").stdout)["changed"])
        self.assertEqual(json.loads(command("v1", "health").stdout)["status"], "success")
        lab.exec("/usr/bin/python3", "-c", f"from pathlib import Path; Path('{root}/srv/mlo/preserve').write_text('keep')")
        self.assertTrue(json.loads(command("v2", "deploy").stdout)["changed"])
        self.assertTrue(json.loads(command("v2", "rollback").stdout)["changed"])
        result = lab.exec("/usr/bin/python3", "-c", f"from pathlib import Path; import json; root=Path('{root}'); assert (root/'opt/mlo/current').resolve().name=='v1'; assert (root/'srv/mlo/preserve').read_text()=='keep'; receipts=list((root/'opt/mlo/receipts').glob('*.json')); assert len(receipts)==5; assert all(json.loads(p.read_text())['status']=='success' for p in receipts); print('linux stdlib install/reapply/health/deploy/rollback PASS')")
        self.assertIn(b"PASS", result.stdout)
        playbook = ["ansible-playbook", "-i", "localhost,", "-c", "local", "/workspace/ansible/install-lab.yml"]
        self.assertIn(b"failed=0", lab.exec(*playbook).stdout)
        second = lab.exec(*playbook).stdout.decode()
        self.assertRegex(second, r"changed=0\s+unreachable=0\s+failed=0")
