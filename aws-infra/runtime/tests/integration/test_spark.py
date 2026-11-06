import unittest
import hashlib
import json
from mlo_runtime.contracts import RUNTIME
from mlo_runtime.lab import Lab


class SparkIntegration(unittest.TestCase):
    def test_actual_spark_delta_partial_sink_retry_checkpoint(self):
        jars = RUNTIME / ".artifacts/jars"
        self.assertEqual(
            len(list(jars.glob("*.jar"))),
            5,
            "run prepare-lab.sh; missing jars are not skipped",
        )
        for item in json.loads((RUNTIME / "connectors.lock.json").read_text())["jars"]:
            self.assertEqual(hashlib.sha256((jars / item["artifact"]).read_bytes()).hexdigest(), item["sha256"])
        lab = Lab("spark")
        self.addCleanup(lab.close)
        lab.configure(
            {
                "image": lab.image("spark"),
                "user": "0:0",
                "mem_limit": "3g",
                "volumes": [
                    "data:/data",
                    f"{jars}:/jars:ro",
                    f"{RUNTIME / 'tests/integration/spark_delta.py'}:/test/spark_delta.py:ro",
                ],
                "entrypoint": ["sleep", "infinity"],
            }
        )
        lab.up()
        version = lab.exec(
            "bash",
            "-c",
            "java -version; ls /opt/spark/jars/hadoop-client-runtime-*.jar",
        )
        (lab.folder / "versions.txt").write_bytes(version.stdout + version.stderr)
        command = [
            "/opt/spark/bin/spark-submit",
            "--master",
            "local[1]",
            "--driver-memory",
            "1g",
            "--jars",
            ",".join("/jars/" + p.name for p in sorted(jars.glob("*.jar"))),
            "/test/spark_delta.py",
        ]
        first = lab.exec(*command, check=False, timeout=180)
        self.assertNotEqual(first.returncode, 0)
        self.assertIn(b"B injected partial sink failure", first.stdout + first.stderr)
        lab.recreate()
        second = lab.exec(*command, timeout=180)
        self.assertIn(b"B_DELTA_CHECKPOINT_RETRY_PASS", second.stdout)


if __name__ == "__main__":
    unittest.main()
