"""Native driver LoadCredential delivery, offline and without executing Spark."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mlo_runtime.native_driver_launch import launch


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class MockExecReached(BaseException):
    pass


class NativeDriverCredentialDelivery(unittest.TestCase):
    def fixture(self, root):
        root = Path(root)
        cred, runtime = root / "credentials", root / "runtime"
        cred.mkdir()
        runtime.mkdir()
        cp = "/opt/mlo/native/connectors/spark-123456789012345678901234/jars/*"
        defaults = root / "spark-defaults.conf"
        defaults_text = (
            "spark.authenticate true\n"
            "spark.authenticate.secret.file /run/mlo/secrets/spark-auth\n"
            "spark.ssl.enabled true\n"
            "spark.ssl.rpc.enabled true\n"
            "spark.network.crypto.saslFallback false\n"
            "spark.driver.extraClassPath " + cp + "\n"
            "spark.executor.extraClassPath " + cp + "\n"
        )
        defaults.write_text(defaults_text)
        defaults.chmod(0o600)
        (cred / "driver-defaults").write_bytes(defaults.read_bytes())
        (cred / "keystore-password").write_text("local-test-only-password")
        (cred / "truststore-password").write_text("local-test-only-password")
        app = root / "application.py"
        app.write_bytes(b"print('test')\n")
        launcher = root / "launch.py"
        launcher.write_bytes(b"# trusted synthetic source\n")
        python_executable = root / "python3.12"
        python_executable.write_bytes(b"synthetic pinned CPython binary\n")
        python_executable.chmod(0o755)
        home = root / "spark-release"
        java = root / "java-release"
        spec = {
            "spark_home": str(home), "java_home": str(java),
            "app": str(app), "app_sha256": sha(app.read_bytes()),
            "launcher": str(launcher), "launcher_sha256": sha(launcher.read_bytes()),
            "python_executable": str(python_executable),
            "python_executable_sha256": sha(python_executable.read_bytes()),
            "spark_defaults": str(defaults), "spark_defaults_sha256": sha(defaults.read_bytes()),
            "connector_classpath": cp,
            "application_artifacts": [],
            "command": [str(home / "bin/spark-submit"), str(app)],
        }
        path = root / "driver.json"
        path.write_text(json.dumps(spec))
        path.chmod(0o600)
        return path, cred, runtime

    def test_root_owned_source_defaults_can_be_delivered_as_systemd_credential(self):
        with tempfile.TemporaryDirectory(prefix="mlo-driver-credentials-") as home:
            spec, cred, runtime = self.fixture(home)
            captured = []

            def fake_exec(path, argv):
                captured.append((path, list(argv), os.environ.get("PYSPARK_PYTHON"),
                                 os.environ.get("PYSPARK_DRIVER_PYTHON")))
                raise MockExecReached

            with patch.dict(os.environ, {
                "CREDENTIALS_DIRECTORY": str(cred),
                "RUNTIME_DIRECTORY": str(runtime),
            }), patch("mlo_runtime.native_driver_launch.os.execv", side_effect=fake_exec):
                with self.assertRaises(MockExecReached):
                    launch(str(spec), sha(spec.read_bytes()))
            self.assertEqual(len(captured), 1)
            approved_python = json.loads(spec.read_text())["python_executable"]
            self.assertEqual(captured[0][2], approved_python)
            self.assertEqual(captured[0][3], approved_python)
            output = runtime / "conf/spark-defaults.conf"
            self.assertTrue(output.is_file())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            text = output.read_text()
            self.assertIn("spark.authenticate.secret.file " + str(cred / "spark-auth"), text)
            self.assertIn("spark.ssl.keyStorePassword local-test-only-password", text)

    def test_python_executable_changed_is_rejected_before_spark_exec(self):
        with tempfile.TemporaryDirectory(prefix="mlo-driver-python-") as home:
            spec, cred, runtime = self.fixture(home)
            python_bin = Path(json.loads(spec.read_text())["python_executable"])
            python_bin.write_bytes(b"modified without release approval")
            with patch.dict(os.environ, {
                "CREDENTIALS_DIRECTORY": str(cred),
                "RUNTIME_DIRECTORY": str(runtime),
            }), patch("mlo_runtime.native_driver_launch.os.execv") as fake_exec:
                with self.assertRaisesRegex(ValueError, "python binary changed"):
                    launch(str(spec), sha(spec.read_bytes()))
                fake_exec.assert_not_called()

    def test_changed_credential_defaults_are_rejected_before_spark_exec(self):
        with tempfile.TemporaryDirectory(prefix="mlo-driver-credentials-") as home:
            spec, cred, runtime = self.fixture(home)
            (cred / "driver-defaults").write_text("spark.authenticate false\n")
            with patch.dict(os.environ, {
                "CREDENTIALS_DIRECTORY": str(cred),
                "RUNTIME_DIRECTORY": str(runtime),
            }), patch("mlo_runtime.native_driver_launch.os.execv") as fake_exec:
                with self.assertRaisesRegex(ValueError, "root-owned defaults changed"):
                    launch(str(spec), sha(spec.read_bytes()))
                fake_exec.assert_not_called()

    def test_changed_accepted_driver_spec_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="mlo-driver-credentials-") as home:
            spec, cred, runtime = self.fixture(home)
            with patch.dict(os.environ, {
                "CREDENTIALS_DIRECTORY": str(cred),
                "RUNTIME_DIRECTORY": str(runtime),
            }), patch("mlo_runtime.native_driver_launch.os.execv") as fake_exec:
                with self.assertRaisesRegex(ValueError, "accepted specification checksum"):
                    launch(str(spec), "0" * 64)
                fake_exec.assert_not_called()


if __name__ == "__main__":
    unittest.main()
