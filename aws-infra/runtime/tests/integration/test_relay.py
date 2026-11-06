"""Real TLS Control -> Gateway A/B -> fake home; no tailnet or GPU is used."""

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from mlo_runtime.home_api import FakeHome, fake_handler
from mlo_runtime.operations import (
    Request,
    UnsafeTransition,
    cancel_status,
    model_status,
    request_model,
)
from mlo_runtime.relay import check_home, relay_handler, tls_opener, tls_server


def certificates(folder):
    """Python 3.14 strict TLS fixture: SKI/AKI/KeyUsage are explicitly present."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    import ipaddress

    now = datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "strict TLS local CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (folder / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    (folder / "server.key").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (folder / "server.key").chmod(0o600)


class RelayIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        certificates(self.folder)
        self.ca = str(self.folder / "ca.crt")
        self.servers = []
        self.addCleanup(self.close_servers)

    def close_servers(self):
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def server(self, handler):
        server = tls_server(
            handler, self.folder / "server.crt", self.folder / "server.key"
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return "https://127.0.0.1:" + str(server.server_port)

    def home(self, delay=0):
        home = FakeHome(self.folder / "home.sqlite", delay=delay)
        # addCleanup is LIFO: finish background jobs before temp directory removal.
        self.addCleanup(home.close)
        url = self.server(fake_handler(home, "home-token"))
        return home, url

    def gateway(self, home, **kwargs):
        return (
            self.server(
                relay_handler(home, "control-token", "home-token", self.ca, **kwargs)
            )
            + "/requests"
        )

    def test_accepted_disconnect_retry_hits_real_home_dedup_once(self):
        home, url = self.home(delay=0.15)
        a = self.gateway(url, disconnect_after_submit=True)
        b = self.gateway(url)
        request = Request(
            "stable-id",
            time.time() + 8,
            "catalog-r1",
            ("approved",),
            {"prompt": "fixture"},
        )
        result = request_model(request, [a, b], "control-token", ca_file=self.ca)
        self.assertEqual(result["candidate"], "approved")
        self.assertEqual(home.received, [request.body(), request.body()])
        self.assertEqual(len(home._threads), 1)
        lookup = model_status("stable-id", [b], "control-token", ca_file=self.ca)
        self.assertEqual(lookup["submissions"], 1)
        self.assertTrue(lookup["gpu_slot_released"])
        self.assertEqual(home.status()["active_slots"], 0)

    def test_lookup_cancel_and_resource_release_are_observable(self):
        home, url = self.home(delay=0.3)
        gateway = self.gateway(url)
        raw = Request("cancel-id", time.time() + 5, "r1", ("x",), {}).body()
        opener = tls_opener(self.ca)
        submit = urllib.request.Request(
            gateway,
            data=raw,
            headers={
                "Authorization": "Bearer control-token",
                "X-Request-ID": "cancel-id",
                "Content-Type": "application/json",
            },
        )
        with opener.open(submit, timeout=2) as response:
            self.assertEqual(response.status, 202)
        observed = model_status(
            "cancel-id", [gateway], "control-token", ca_file=self.ca
        )
        self.assertEqual(observed["state"], "running")
        self.assertFalse(observed["gpu_slot_released"])
        cancelled = model_status(
            "cancel-id", [gateway], "control-token", cancel=True, ca_file=self.ca
        )
        self.assertIn(cancelled["state"], {"requested", "cancelled"})
        home.close()
        released = model_status(
            "cancel-id", [gateway], "control-token", ca_file=self.ca
        )
        self.assertEqual(released["state"], "cancelled")
        self.assertEqual(
            cancel_status(released), {"acknowledged": True, "gpu_slot_released": True}
        )
        self.assertEqual(home.status()["active_slots"], 0)

    def test_late_result_times_out_then_original_id_reconciles_expiry(self):
        home, url = self.home(delay=0.25)
        gateway = self.gateway(url)
        with self.assertRaises((TimeoutError, UnsafeTransition)):
            request_model(
                Request("late-id", time.time() + 0.12, "r1", ("x",), {}),
                [gateway],
                "control-token",
                ca_file=self.ca,
            )
        home.close()
        result = model_status("late-id", [gateway], "control-token", ca_file=self.ca)
        self.assertEqual(result["state"], "expired")
        self.assertTrue(result["gpu_slot_released"])
        self.assertEqual(len(home._threads), 1)

    def test_wrong_auth_and_untrusted_tls_do_not_reach_home(self):
        home, url = self.home()
        self.assertTrue(check_home(url, "home-token", self.ca)["ready"])
        gateway = self.gateway(url)
        request = Request("auth-id", time.time() + 3, "r1", ("x",), {})
        with self.assertRaises(UnsafeTransition):
            request_model(request, [gateway], "wrong", ca_file=self.ca)
        with self.assertRaises(UnsafeTransition):
            request_model(request, [gateway], "control-token")
        self.assertEqual(home.received, [])


if __name__ == "__main__":
    unittest.main()
