"""Disposable Docker Compose lab helpers; only clean resources this run created."""

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .contracts import RUNTIME


class Lab:
    def __init__(self, name):
        self.project = "mlo-p4-b-" + name + "-" + uuid.uuid4().hex[:10]
        self.folder = RUNTIME / ".artifacts" / self.project
        self.folder.mkdir(parents=True, mode=0o700)
        self.compose_path = self.folder / "compose.json"
        self.service = name
        self.locks = json.loads((RUNTIME / "images.lock.json").read_text())["images"]

    def image(self, name):
        item = self.locks[name]
        return item["ref"] + "@" + item["digest"]

    def docker(self, *args, input=None, timeout=300, check=True):
        result = subprocess.run(
            ["docker", *map(str, args)],
            input=input,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        with (self.folder / "commands.log").open("ab") as stream:
            stream.write(
                ("docker " + " ".join(map(str, args)) + "\n").encode()
                + result.stdout
                + result.stderr
            )
        if check and result.returncode:
            raise RuntimeError(
                f"Docker command failed ({result.returncode}); see {self.folder / 'commands.log'}"
            )
        return result

    def compose(self, *args, **kwargs):
        return self.docker(
            "compose",
            "--project-name",
            self.project,
            "--file",
            self.compose_path,
            *args,
            **kwargs,
        )

    def configure(self, service):
        service.setdefault("labels", {})["mlo.role"] = "B"
        service["labels"]["mlo.run"] = self.project
        service.setdefault("mem_limit", "2g")
        service.setdefault("cpus", 2)
        service.setdefault("pids_limit", 1024)
        # Native ARM execution is explicitly separate from the rendered EC2 amd64 plan.
        service["platform"] = os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64")
        self.compose_path.write_text(
            json.dumps(
                {"services": {self.service: service}, "volumes": {"data": {}}}, indent=2
            )
        )

    def up(self):
        self.compose("up", "-d", timeout=600)

    def exec(self, *args, input=None, check=True, timeout=120):
        return self.compose(
            "exec", "-T", self.service, *args, input=input, check=check, timeout=timeout
        )

    def ready(self, command, attempts=90):
        for _ in range(attempts):
            result = self.exec(*command, check=False, timeout=10)
            if result.returncode == 0:
                return
            state = self.compose(
                "ps", "--all", "--format", "json", check=False
            ).stdout.decode()
            if '"State":"exited"' in state:
                self.compose("logs", "--no-color", check=False)
                raise RuntimeError(f"service exited before readiness: {self.folder}")
            time.sleep(1)
        self.compose("logs", "--no-color", check=False)
        raise RuntimeError(f"service readiness timed out: {self.folder}")

    def recreate(self):
        self.compose("up", "-d", "--force-recreate", timeout=120)

    def close(self):
        if self.compose_path.exists():
            self.compose("logs", "--no-color", check=False)
            # These volumes only belong to the project created above, never global prune.
            self.compose("down", "--volumes", "--remove-orphans", timeout=120)


def certificates(folder):
    """Ephemeral lab-only CA/server certificate; no production identity is generated."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    import datetime
    import ipaddress

    folder = Path(folder)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "B ephemeral local lab")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
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
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("broker"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                    x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_bytes = server_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_bytes = server.public_bytes(serialization.Encoding.PEM)
    (folder / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (folder / "server.crt").write_bytes(cert_bytes)
    (folder / "server.key").write_bytes(key_bytes)
    (folder / "server.pem").write_bytes(key_bytes + cert_bytes)
    (folder / "server.p12").write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"server",
            server_key,
            server,
            [ca],
            serialization.BestAvailableEncryption(b"fixture"),
        )
    )
    # Only disposable container-visible fixtures. Directory itself is private on host.
    for p in folder.glob("server.*"):
        p.chmod(0o644)
