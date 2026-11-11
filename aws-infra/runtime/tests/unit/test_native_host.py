import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from mlo_runtime.contracts import ROOT, load_inventory
from mlo_runtime.install import InstallError, sha
from mlo_runtime.native_host import NativeHost, execute, verify_payload
from mlo_runtime.native_package import package
from mlo_runtime.native_releases import extract, lock, verify
from mlo_runtime.native_connectors import release_id, classpath, lock_sha256
from mlo_runtime.native_execution import SystemdSparkExecution, identity, driver_unit
from mlo_runtime.native_execution import NativeAdmission
from mlo_runtime.native_fence import dispatch
from mlo_runtime.operations import UnsafeTransition


class NativeArchives(unittest.TestCase):
    def archive(self, root, entries):
        archive = root / "archive.tgz"
        with tarfile.open(archive, "w:gz") as stream:
            for name, body, kind in entries:
                item = tarfile.TarInfo(name)
                item.mode = 0o755
                if kind == "link":
                    item.type, item.linkname = tarfile.SYMTYPE, body
                    stream.addfile(item)
                else:
                    item.size = len(body)
                    stream.addfile(item, io.BytesIO(body))
        item = {"algorithm": "sha256", "digest": sha(archive), "root": "release", "executables": ["bin/run"]}
        return archive, item

    def test_publisher_lock_is_pinned_and_has_full_native_closure(self):
        value = lock(ROOT / "aws-infra/runtime/native-releases.lock.json")
        self.assertEqual(len(value["releases"]), 5)
        for item in value["releases"].values():
            self.assertNotIn("latest", item["url"])
            self.assertTrue(item["digest"])

    def test_extract_safe_link_as_regular_file_and_preserve_executable(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive, item = self.archive(root, [("release/bin/run", b"#!/bin/sh\nexit 0\n", "file"), ("release/bin/alias", "run", "link")])
            extract(archive, item, root / "installed")
            self.assertEqual((root / "installed").stat().st_mode & 0o777, 0o755)
            self.assertFalse((root / "installed/bin/alias").is_symlink())
            self.assertEqual((root / "installed/bin/alias").read_bytes(), (root / "installed/bin/run").read_bytes())
            with self.assertRaises(ValueError):
                extract(archive, item, root / "installed")

    def test_checksum_corruption_and_escaping_archive_are_refused_before_publish(self):
        for entry in [("release/../../escape", b"x", "file"), ("release/bin/escape", "../../../escape", "link")]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                archive, item = self.archive(root, [("release/bin/run", b"run", "file"), entry])
                with self.assertRaises(ValueError):
                    extract(archive, item, root / "installed")
                self.assertFalse((root / "installed").exists())
                archive.write_bytes(archive.read_bytes() + b"corruption")
                with self.assertRaises(ValueError):
                    verify(archive, item)


class NativeInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mlo-native-unit-")
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.inventory, _ = load_inventory(ROOT / "aws-infra/contracts/inventory.example.json")
        result = package(self.inventory, self.root / "native.tgz", "native-test-v1", fixture=True)
        from mlo_runtime.artifact import inspect_artifact
        self.payload = self.root / "payload"
        inspect_artifact(self.root / "native.tgz", result["sha256"], self.inventory["environment"], self.inventory["generation"], "kafka-01", self.payload, result["manifest_sha256"])
        manifest = json.loads((self.payload / "manifest.json").read_text())
        self.binding = {"schema_version": "1.0.0", "lab": True, "root": str(self.root),
            "execution_target": "native-systemd-v1", "instance_id": "i-12345678", "node": "kafka-01", "role": "kafka",
            "generation": self.inventory["generation"], "environment": self.inventory["environment"],
            "accepted_manifests": [result["manifest_sha256"]], "required_secrets": manifest["required_secrets"]["kafka-01"],
            "image_digests": manifest["image_digests"],
            "native_release_lock_sha256": sha(ROOT / "aws-infra/runtime/native-releases.lock.json"),
            "native_connector_lock_sha256": sha(ROOT / "aws-infra/runtime/native-connectors.lock.json")}
        self.binding_path = self.root / "binding.json"
        self.save_binding()
        for name in self.binding["required_secrets"]:
            target = self.root / name.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic-test-credential")
            target.chmod(0o600)

    def save_binding(self):
        self.binding_path.write_text(json.dumps(self.binding))
        self.binding_path.chmod(0o600)

    def test_native_package_roundtrip_and_tampered_payload_or_release_lock_refusal(self):
        manifest, node = verify_payload(self.payload, self.binding, "install")
        self.assertEqual(len(manifest["target_nodes"]), 9)
        self.assertEqual(set(manifest["image_digests"]), {"filebeat", "host-proxy"})
        self.assertEqual(node["revision"], "native-test-v1")
        wrong = {**self.binding, "native_release_lock_sha256": "0" * 64}
        with self.assertRaises(InstallError):
            verify_payload(self.payload, wrong, "install")
        wrong_connector = {**self.binding, "native_connector_lock_sha256": "0" * 64}
        with self.assertRaises(InstallError):
            verify_payload(self.payload, wrong_connector, "install")
        (self.payload / "hosts/kafka-01/native.json").write_text("{}")
        with self.assertRaises(InstallError):
            verify_payload(self.payload, self.binding, "install")

    def test_install_stages_without_start_and_crash_state_blocks_replay(self):
        calls = []
        class Backend(NativeHost):
            def mount(self):
                calls.append("mount")
            def preflight(self):
                calls.append("preflight")
            def start(self, node):
                raise AssertionError("install must not start a service")
        backend = Backend(self.binding)
        with patch("mlo_runtime.native_host.stage", side_effect=lambda *args: calls.append("stage")):
            result = execute(self.payload, self.binding_path, "install", self.binding["environment"], self.binding["generation"], "kafka-01", backend=backend)
        self.assertEqual(calls, ["mount", "preflight", "stage"])
        self.assertTrue(result["changed"])
        state_path = backend.path("/var/lib/mlo/native-state.json")
        state = json.loads(state_path.read_text())
        state["pending"] = {"operation": "deploy", "revision": "native-test-v1"}
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(InstallError, "reconciliation"):
            execute(self.payload, self.binding_path, "deploy", self.binding["environment"], self.binding["generation"], "kafka-01", backend=backend)

    def test_wrong_mount_uuid_and_nested_cgroup_process_block_success(self):
        backend = NativeHost({**self.binding, "lab": False, "mount_uuid": "expected"})
        backend.command = lambda argv, **kw: (str(self.root / "srv/mlo") if argv[-1] == "TARGET" else "wrong").encode()
        with self.assertRaises(InstallError):
            backend.mount()
        group = self.root / "sys/fs/cgroup/system.slice/mlo-kafka.service"
        (group / "child").mkdir(parents=True)
        (group / "cgroup.events").write_text("populated 1\n")
        (group / "child/cgroup.procs").write_text("123\n")
        backend.command = lambda argv, **kw: b"LoadState=loaded\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=/system.slice/mlo-kafka.service\nKillMode=control-group\n"
        with self.assertRaises(InstallError):
            backend.stopped(["mlo-kafka.service"])
        (group / "child/cgroup.procs").write_text("")
        (group / "cgroup.events").write_text("populated 0\n")
        backend.stopped(["mlo-kafka.service"])


class NativeDrivers(unittest.TestCase):
    def test_partial_observation_still_stops_all_known_writers_and_blocks_takeover(self):
        with tempfile.TemporaryDirectory() as folder:
            token = {"owner": "control-a", "generation": "g1", "epoch": 1, "expires_at": 100}
            names = {q: "mlo-driver-" + identity(token, q)[:32] + ".service" for q in ("weather-stream", "feedback-postgresql")}
            registry = Path(folder) / "registry.json"
            registry.write_text(json.dumps({**{k: token[k] for k in ("owner", "generation", "epoch")}, "units": names}))
            registry.chmod(0o600)
            calls = []
            class Host:
                def command(self, argv):
                    calls.append(argv)
                    if len(calls) == 1:
                        raise InstallError("unknown first unit")
            binding = {"execution_backend": "native-systemd-v1", "owner": "control-a", "generation": "g1", "registry": str(registry),
                       "masters": ["https://m1", "https://m2", "https://m3"], "workers": ["https://w1", "https://w2", "https://w3"],
                       "ca_file": "ca", "client_cert": "cert", "client_key": "key"}
            execution = SystemdSparkExecution(binding, Host())
            with patch.object(execution, "units", side_effect=UnsafeTransition("partial observation")), self.assertRaises(UnsafeTransition):
                execution.stop_group(token)
            self.assertEqual(calls, [["systemctl", "stop", name] for name in names.values()])

    def test_admission_is_idempotent_and_lease_loss_after_start_stops_the_unit(self):
        for revoke in (False, True):
            with self.subTest(revoke=revoke), tempfile.TemporaryDirectory() as folder:
                root, calls = Path(folder), []
                token = {"owner": "control-a", "generation": "g1", "epoch": 1, "expires_at": 100}
                class Lease:
                    lost = False
                    def read(self):
                        return None if self.lost else token
                    def now(self):
                        return 90
                lease = Lease()
                class Barrier:
                    def check(self):
                        return None
                class Host:
                    def path(self, name):
                        return root / name.lstrip("/")
                    def mount(self):
                        return None
                    def command(self, argv):
                        calls.append(argv)
                        if argv[:2] == ["systemctl", "start"] and revoke:
                            lease.lost = True
                        return b""
                    def stopped(self, units):
                        calls.append(["observed-stopped", *units])
                host = Host()
                spark_release = lock(ROOT / "aws-infra/runtime/native-releases.lock.json")["releases"]
                spark_digest, java_digest = spark_release["spark"]["digest"], spark_release["java21"]["digest"]
                connector_digest = lock_sha256(ROOT / "aws-infra/runtime/native-connectors.lock.json")
                runtime_id = release_id(spark_digest, connector_digest)
                connector_cp = classpath("/opt/mlo/native", runtime_id)
                spec = {"query": "weather-stream", "generation": "g1", "app": "/opt/mlo/current/apps/app.py",
                        "launcher": "/opt/mlo/native/helpers/test/native_driver_launch.py",
                        "spark_defaults": "/etc/mlo-native/driver/spark-defaults.conf",
                        "python_executable": "/opt/mlo/native/python/feedback-lab/venv/bin/python3.12",
                        "spark_home": "/opt/mlo/native/releases/spark-" + spark_digest[:16],
                        "java_home": "/opt/mlo/native/releases/java21-" + java_digest[:16],
                        "native_connector_lock_sha256": connector_digest,
                        "spark_runtime_release_id": runtime_id,
                        "connector_classpath": connector_cp,
                        "credentials": ["spark-auth", "keystore-password", "truststore-password"]}
                for field in ("app", "launcher", "spark_defaults", "python_executable"):
                    source = host.path(spec[field])
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if field == "spark_defaults":
                        source.write_text("spark.authenticate true\nspark.authenticate.secret.file /run/mlo/secrets/spark-auth\n"
                                          "spark.ssl.enabled true\nspark.ssl.rpc.enabled true\n"
                                          "spark.network.crypto.saslFallback false\n"
                                          "spark.driver.extraClassPath " + connector_cp + "\n"
                                          "spark.executor.extraClassPath " + connector_cp + "\n")
                    else:
                        source.write_text("synthetic fixture\n")
                    spec[field + "_sha256"] = sha(source)
                archive = host.path("/opt/mlo/current/apps/accepted-code.zip")
                archive.parent.mkdir(parents=True, exist_ok=True)
                archive.write_bytes(b"approved synthetic application archive")
                spec["application_artifacts"] = [{"path": "/opt/mlo/current/apps/accepted-code.zip",
                                                   "sha256": sha(archive)}]
                spec["command"] = [spec["spark_home"] + "/bin/spark-submit", "--py-files",
                                   "/opt/mlo/current/apps/accepted-code.zip", spec["app"]]
                source = root / "spec.json"
                source.write_text(json.dumps(spec))
                source.chmod(0o600)
                connector_path = host.path("/etc/mlo/native-connectors.lock.json")
                connector_path.parent.mkdir(parents=True, exist_ok=True)
                connector_path.write_bytes((ROOT / "aws-infra/runtime/native-connectors.lock.json").read_bytes())
                connector_path.chmod(0o600)
                binding = {"lab": True, "root": str(root), "owner": "control-a", "generation": "g1",
                           "registry": str(root / "registry.json"), "accepted_driver_specs": [sha(source)],
                           "native_connector_lock_path": "/etc/mlo/native-connectors.lock.json",
                           "native_connector_lock_sha256": connector_digest,
                           "spark_publisher_digest": spark_digest, "java21_publisher_digest": java_digest}
                admission = NativeAdmission(binding, lease, Barrier(), host)
                accepted_cp = classpath(host.path("/opt/mlo/native"), runtime_id)
                with patch("mlo_runtime.native_execution.verify_bundle", return_value={"connector_classpath": accepted_cp}):
                    if revoke:
                        with self.assertRaises(UnsafeTransition):
                            admission.admit(token, "weather-stream", source, sha(source))
                        self.assertTrue(any(c[0] == "observed-stopped" for c in calls))
                        self.assertIsNotNone(json.loads((root / "registry.json").read_text())["pending"])
                    else:
                        archive.write_bytes(b"tampered synthetic application archive")
                        with self.assertRaises(UnsafeTransition):
                            admission.admit(token, "weather-stream", source, sha(source))
                        self.assertFalse((root / "registry.json").exists())
                        archive.write_bytes(b"approved synthetic application archive")
                        first = admission.admit(token, "weather-stream", source, sha(source))
                        second = admission.admit(token, "weather-stream", source, sha(source))
                        self.assertTrue(first["started"])
                        self.assertFalse(second["started"])
                        self.assertEqual(sum(c[:2] == ["systemctl", "start"] for c in calls), 1)
                        unit = host.path("/etc/systemd/system/" + first["unit"]).read_text()
                        self.assertIn("Restart=no\n", unit)
                        self.assertIn("KillMode=control-group\n", unit)
                        self.assertIn("LoadCredential=driver-spec:" + str(source), unit)
                        self.assertIn("LoadCredential=driver-defaults:" + spec["spark_defaults"], unit)
                        self.assertIn('"%d/driver-spec"', unit)
                        self.assertNotIn('"' + str(source) + '" ', next(x for x in unit.splitlines() if x.startswith("ExecStart=")))

    def test_expired_or_replaced_lease_denies_native_admission_before_files_or_start(self):
        token = {"owner": "control-a", "generation": "g1", "epoch": 1, "expires_at": 100}
        class Lease:
            def read(self):
                return token
            def now(self):
                return 99
        class Barrier:
            def check(self):
                return None
        class Host:
            def mount(self):
                raise AssertionError("expiry must be rejected before mount/start")
        with tempfile.TemporaryDirectory() as folder:
            admission = NativeAdmission({"owner": "control-a", "generation": "g1", "registry": str(Path(folder) / "registry.json")}, Lease(), Barrier(), Host())
            with self.assertRaises(UnsafeTransition):
                admission.admit(token, "weather-stream", "/missing", "0" * 64)
            with self.assertRaises(UnsafeTransition):
                admission.admit({**token, "epoch": 2}, "weather-stream", "/missing", "0" * 64)

    def test_missing_registry_for_previous_lease_cannot_be_cold_start_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            binding = {"registry": str(Path(folder) / "missing")}
            class Lease:
                def read(self):
                    return {"epoch": 1}
            with self.assertRaises(UnsafeTransition):
                dispatch(binding, Lease(), None, {}, "observe", execution=object())

    def test_driver_unit_is_epoch_scoped_and_cannot_restart_without_admission(self):
        token = {"owner": "control-a", "generation": "g1", "epoch": 1, "expires_at": 9999999999}
        digest = identity(token, "weather-stream")
        self.assertNotEqual(digest, identity({**token, "epoch": 2}, "weather-stream"))
        argv = driver_unit(token, "weather-stream", ["/opt/mlo/native/spark-driver/bin/spark-submit", "app.py"], credentials=["spark-auth"], working_directory="/srv/mlo/g1/spark-driver")
        self.assertIn("--property=Restart=no", argv)
        self.assertIn("--property=KillMode=control-group", argv)
        self.assertFalse(any("docker" in p for p in argv))
        with self.assertRaises(UnsafeTransition):
            driver_unit(token, "weather-stream", ["/bin/sh", "bad"], credentials=[], working_directory="/srv/mlo/g1/spark-driver")


if __name__ == "__main__":
    unittest.main()
