"""Disposable Docker Compose lab helpers; only clean resources this run created."""

from __future__ import annotations
import json
import hashlib
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
        self._image_volumes = {}

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
        if args and args[0] in {"up", "run", "create"}:
            self._own_image_volumes()
        return self.docker(
            "compose",
            "--project-name",
            self.project,
            "--file",
            self.compose_path,
            *args,
            **kwargs,
        )

    def _own_image_volumes(self):
        """Name image-declared volumes so recreation cannot orphan anonymous data."""
        config = json.loads(self.compose_path.read_text())
        volumes = config.setdefault("volumes", {})
        owned = []
        for name, service in config["services"].items():
            image = service["image"]
            if image not in self._image_volumes:
                raw = self.docker("image", "inspect", image, "--format", "{{json .Config.Volumes}}")
                self._image_volumes[image] = json.loads(raw.stdout) or {}
            mounts = service.setdefault("volumes", [])
            targets = set()

            def named(target):
                key = "image_" + hashlib.sha256((name + "\0" + target).encode()).hexdigest()[:16]
                volumes.setdefault(key, {"labels": {"mlo.role": "B", "mlo.run": self.project}})
                owned.append({"service": name, "image": image, "target": target, "volume": key})
                return key

            for index, mount in enumerate(mounts):
                if isinstance(mount, str):
                    parts = mount.split(":")
                    target = parts[-1] if parts[-1].startswith("/") else parts[-2]
                    if len(parts) == 1:
                        mounts[index] = named(target) + ":" + target
                else:
                    target = mount["target"]
                    if mount.get("type", "volume") == "volume" and not mount.get("source"):
                        mount["source"] = named(target)
                targets.add(target.rstrip("/"))
            for target in sorted(self._image_volumes[image]):
                if not target.startswith("/"):
                    raise RuntimeError("image volume target must be absolute")
                # A bind on /data/db does not cover an image VOLUME /data.
                if any(target == base or target.startswith(base + "/") for base in targets):
                    continue
                mounts.append(named(target) + ":" + target)
                targets.add(target.rstrip("/"))
        self.compose_path.write_text(json.dumps(config, indent=2))
        if owned:
            (self.folder / "image-volumes.json").write_text(json.dumps(owned, indent=2))

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
            # A multi-service outage lab deliberately keeps peers stopped.
            # Diagnose the service being probed, not every project container.
            state = self.compose(
                "ps", "--all", "--format", "json", self.service, check=False
            ).stdout.decode()
            if '"State":"exited"' in state:
                self.compose("logs", "--no-color", check=False)
                raise RuntimeError(f"service exited before readiness: {self.folder}")
            time.sleep(1)
        self.compose("logs", "--no-color", check=False)
        raise RuntimeError(f"service readiness timed out: {self.folder}")

    def recreate(self):
        self.compose("up", "-d", "--force-recreate", timeout=120)

    def close(self, *, timeout=120):
        if self.compose_path.exists():
            self.compose("logs", "--no-color", check=False)
            # These volumes only belong to the project created above, never global prune.
            self.compose("down", "--volumes", "--remove-orphans", timeout=timeout)
            remaining = self.docker("volume", "ls", "--filter",
                                    "label=com.docker.compose.project=" + self.project,
                                    "--format", "{{.Name}}")
            if remaining.stdout.strip():
                raise RuntimeError("owned lab volumes remain; inspect " + str(self.folder))


def certificates(folder, dns_names=()):
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                    key_encipherment=False, data_encipherment=False,
                                    key_agreement=False, key_cert_sign=True, crl_sign=True,
                                    encipher_only=False, decipher_only=False), True)
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
                    *[x509.DNSName(name) for name in dns_names],
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
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
