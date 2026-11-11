"""Ephemeral local mTLS/PKCS12 check (not real AWS PKI or systemd acceptance)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def issue(key, name, issuer_key, issuer_name, *, ca, usage=None):
    now = datetime.now(timezone.utc)
    name_obj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer = issuer_name or name_obj
    builder = (x509.CertificateBuilder().subject_name(name_obj).issuer_name(issuer)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=2))
               .not_valid_after(now + timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None), critical=True))
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def pem_key(key):
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class NativeLocalPki(unittest.TestCase):
    def test_ephemeral_mtls_and_pkcs12_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="mlo-pki-probe-") as path:
            home = Path(path)
            ca_key = ec.generate_private_key(ec.SECP256R1())
            ca_cert = issue(ca_key, "MLO Local Test CA", ca_key, None, ca=True)
            server_key = ec.generate_private_key(ec.SECP256R1())
            server_cert = issue(server_key, "spark-master.native.test", ca_key,
                                ca_cert.subject, ca=False, usage=ExtendedKeyUsageOID.SERVER_AUTH)
            client_key = ec.generate_private_key(ec.SECP256R1())
            client_cert = issue(client_key, "control.native.test", ca_key,
                                ca_cert.subject, ca=False, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
            certs = {
                "ca.pem": ca_cert.public_bytes(serialization.Encoding.PEM),
                "server.pem": server_cert.public_bytes(serialization.Encoding.PEM),
                "server.key": pem_key(server_key),
                "client.pem": client_cert.public_bytes(serialization.Encoding.PEM),
                "client.key": pem_key(client_key),
            }
            for name, data in certs.items():
                target = home / name
                target.write_bytes(data)
                target.chmod(0o600)
            password = b"mlo-local-only-pkcs12-test"
            bundle = pkcs12.serialize_key_and_certificates(
                b"mlo-local-client", client_key, client_cert, [ca_cert],
                serialization.BestAvailableEncryption(password))
            test_p12 = home / "client.p12"
            test_p12.write_bytes(bundle)
            test_p12.chmod(0o600)
            key, cert, chain = pkcs12.load_key_and_certificates(test_p12.read_bytes(), password)
            self.assertIsNotNone(key)
            self.assertEqual(cert.subject, client_cert.subject)
            self.assertEqual(chain[0].subject, ca_cert.subject)

            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(str(home / "server.pem"), str(home / "server.key"))
            server_context.load_verify_locations(cafile=str(home / "ca.pem"))
            server_context.verify_mode = ssl.CERT_REQUIRED
            client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_context.load_cert_chain(str(home / "client.pem"), str(home / "client.key"))
            client_context.load_verify_locations(cafile=str(home / "ca.pem"))
            client_context.verify_mode = ssl.CERT_REQUIRED
            client_context.check_hostname = True

            def exchange(ctx):
                listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listening.settimeout(8)
                listening.bind(("127.0.0.1", 0))
                listening.listen(1)
                port = listening.getsockname()[1]
                server_results = []

                def serve():
                    try:
                        conn, _ = listening.accept()
                        with server_context.wrap_socket(conn, server_side=True) as tls:
                            self.assertIsNotNone(tls.getpeercert())
                            tls.sendall(b"tls-ok")
                        server_results.append(True)
                    except ssl.SSLError:
                        server_results.append(False)
                    finally:
                        listening.close()
                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=8) as connection:
                        with ctx.wrap_socket(connection, server_hostname="spark-master.native.test") as tls:
                            try:
                                value = tls.recv(6)
                            except ssl.SSLError:
                                value = b""
                except ssl.SSLError:
                    value = b""
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                return value, server_results

            received, results = exchange(client_context)
            self.assertEqual(received, b"tls-ok")
            self.assertEqual(results, [True])
            unauthenticated = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            unauthenticated.load_verify_locations(cafile=str(home / "ca.pem"))
            unauthenticated.check_hostname = True
            unauthenticated.verify_mode = ssl.CERT_REQUIRED
            received, results = exchange(unauthenticated)
            self.assertEqual(received, b"")
            self.assertEqual(results, [False])


if __name__ == "__main__":
    unittest.main()
