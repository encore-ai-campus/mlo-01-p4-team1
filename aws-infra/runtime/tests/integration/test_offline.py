import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import yaml

from mlo_runtime.contracts import ROOT, RUNTIME, load_inventory
from mlo_runtime.operations import Request, UnsafeTransition, request_model, rollout


class Server:
    def __init__(self, **options):
        command = [sys.executable, str(Path(__file__).with_name("smoke_http.py"))]
        for key, value in options.items():
            command += ["--" + key.replace("_", "-"), str(value)]
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        port = self.process.stdout.readline()
        if not port:
            _, error = self.process.communicate(timeout=5)
            raise RuntimeError("HTTP smoke process failed: " + error)
        self.port = int(port)
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.communicate(timeout=5)


class OfflineIntegration(unittest.TestCase):
    def server(self, **kwargs):
        server = Server(**kwargs)
        self.addCleanup(server.close)
        return server

    def test_16_nodes_via_real_shell_adapter_renderer_shared_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            output, bundle = Path(directory) / "plan.json", Path(directory) / "bundle"
            subprocess.run(
                [
                    "bash",
                    str(RUNTIME / "scripts/render-fixture.sh"),
                    "--inventory",
                    str(ROOT / "aws-infra/contracts/inventory.example.json"),
                    "--output",
                    str(output),
                    "--bundle",
                    str(bundle),
                ],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_preparation.py"),
                    "--runtime-plan",
                    str(output),
                ],
                check=True,
            )
            inventory, _ = load_inventory(
                ROOT / "aws-infra/contracts/inventory.example.json"
            )
            self.assertEqual(
                {p.name for p in (bundle / "hosts").iterdir()},
                {n["name"] for n in inventory["nodes"]},
            )
            for file in bundle.glob("hosts/*/compose.yml"):
                self.assertIn("services", yaml.safe_load(file.read_text()))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                subprocess.run(
                    [
                        "docker",
                        "compose",
                        "--file",
                        str(file),
                        "config",
                        "--no-interpolate",
                        "--no-path-resolution",
                        "--format",
                        "json",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )

    def test_model_failover_keeps_identical_id_body_deadline_and_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "requests.jsonl"
            a = self.server(identity="a", status=503, capture=capture)
            b = self.server(identity="b", capture=capture)
            request = Request(
                "same-id",
                time.time() + 10,
                "catalog-v1",
                ("approved",),
                {"prompt": "smoke"},
            )
            result = request_model(request, [a.url, b.url], "smoke")
            self.assertEqual(result["candidate"], "approved")
            lines = capture.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], lines[1])
            a.close()
            self.assertEqual(
                request_model(request, [a.url, b.url], "smoke")["request_id"], "same-id"
            )
            with self.assertRaises(UnsafeTransition):
                request_model(request, [b.url], "wrong-auth")

    def test_429_and_late_response_do_not_imply_gpu_release(self):
        busy, good = self.server(status=429), self.server()
        request = Request(
            "retry-id", time.time() + 5, "r", ("x",), {"context": "smoke"}
        )
        self.assertEqual(
            request_model(request, [busy.url, good.url], "smoke")["request_id"],
            "retry-id",
        )
        delayed = self.server(delay=0.15)
        with self.assertRaises((TimeoutError, UnsafeTransition)):
            request_model(
                Request("late", time.time() + 0.03, "r", ("x",), {}),
                [delayed.url],
                "smoke",
            )

    def test_real_http_forward_proxy_rejects_auth_then_b_handles_new_request(self):
        upstream = self.server(identity="destination")
        a, b = self.server(identity="a"), self.server(identity="b")

        def via(proxy, auth):
            # http.client sends the absolute URI through an actual separate proxy process.
            import http.client

            connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=1)
            try:
                connection.request(
                    "GET",
                    upstream.url + "/ready",
                    headers={"Proxy-Authorization": auth},
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        self.assertEqual(via(a, "wrong")[0], 407)
        self.assertEqual(via(a, "Bearer smoke")[1]["proxy"], "a")
        a.close()
        with self.assertRaises(ConnectionError):
            via(a, "Bearer smoke")
        self.assertEqual(via(b, "Bearer smoke")[1]["proxy"], "b")

    def test_web_process_readiness_failure_restores_previous_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            revisions, servers = {}, {}
            for name in ["a", "b"]:
                revisions[name] = Path(directory) / name
                revisions[name].write_text("old")
                servers[name] = self.server(revision=revisions[name])

            class Backend:
                targets = {"a", "b"}

                def snapshot(self, n):
                    return revisions[n].read_text()

                def check_migration(self, n, old, new):
                    pass

                def drain(self, n):
                    self.targets.discard(n)

                def wait_drained(self, n):
                    if n in self.targets:
                        raise RuntimeError("still routed")

                def replace_web(self, n, revision):
                    revisions[n].write_text(
                        "bad" if n == "b" and revision == "new" else revision
                    )

                def wait_ready(self, n, revision):
                    with urllib.request.urlopen(
                        servers[n].url + "/ready", timeout=1
                    ) as response:
                        if json.loads(response.read())["revision"] != revision:
                            raise RuntimeError("wrong revision")

                def register(self, n):
                    self.targets.add(n)

                def wait_healthy(self, n):
                    self.wait_ready(n, revisions[n].read_text())

            backend = Backend()
            with self.assertRaises(UnsafeTransition):
                rollout(["a", "b"], "new", backend)
            self.assertEqual(
                [p.read_text() for p in revisions.values()], ["old", "old"]
            )
            self.assertEqual(backend.targets, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
