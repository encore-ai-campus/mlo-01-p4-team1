"""Three real TLS ZooKeeper peers, quorum loss/recovery and persistent restart."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from mlo_runtime.lab import Lab
from mlo_runtime.install import validate_identity


def quorum_certificates(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MLO disposable quorum CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
          .not_valid_after(now+datetime.timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True).sign(key, hashes.SHA256()))
    (folder / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for peer in ["z1", "z2", "z3"]:
        peerkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, peer)]))
                       .issuer_name(name).public_key(peerkey.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now-datetime.timedelta(minutes=1)).not_valid_after(now+datetime.timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName(peer), x509.DNSName("localhost")]), False)
                       .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH, x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), False)
                       .sign(key, hashes.SHA256()))
        (folder / (peer + ".p12")).write_bytes(pkcs12.serialize_key_and_certificates(peer.encode(), peerkey, certificate, [ca], serialization.BestAvailableEncryption(b"fixture")))


def quorum_services(lab):
    quorum_certificates(lab.folder)
    services = {}
    for sid in [1, 2, 3]:
        peer = "z" + str(sid)
        data = lab.folder / ("data-" + peer)
        validate_identity("spark", "spark-0" + str(sid), data, {"node_id": sid})
        conf = ("tickTime=2000\ninitLimit=10\nsyncLimit=5\ndataDir=/data/zookeeper\nsecureClientPort=2182\n"
                "serverCnxnFactory=org.apache.zookeeper.server.NettyServerCnxnFactory\nsslQuorum=true\nportUnification=false\n"
                "ssl.clientAuth=need\nssl.quorum.clientAuth=need\nssl.keyStore.type=PKCS12\nssl.trustStore.type=PKCS12\n"
                f"ssl.keyStore.location=/seed/{peer}.p12\nssl.keyStore.password=fixture\nssl.trustStore.location=/tmp/truststore.p12\nssl.trustStore.password=fixture\n"
                "ssl.quorum.keyStore.type=PKCS12\nssl.quorum.trustStore.type=PKCS12\n"
                f"ssl.quorum.keyStore.location=/seed/{peer}.p12\nssl.quorum.keyStore.password=fixture\nssl.quorum.trustStore.location=/tmp/truststore.p12\nssl.quorum.trustStore.password=fixture\n"
                "4lw.commands.whitelist=ruok,srvr,mntr\n" + "".join(f"server.{i}=z{i}:2888:3888\n" for i in [1,2,3]))
        (lab.folder / (peer + ".cfg")).write_text(conf)
        services[peer] = {"image": lab.image("zookeeper"), "platform": os.environ.get("RUNTIME_LAB_PLATFORM", "linux/arm64"), "hostname": peer,
                          "user": "0:0", "mem_limit": "512m", "environment": {"JVMFLAGS": "-Xms64m -Xmx128m"},
                          "volumes": [f"{lab.folder}:/seed:ro", f"{data}/zookeeper:/data/zookeeper"],
                          "entrypoint": ["/bin/bash", "-ec", f"if [ ! -f /tmp/truststore.p12 ]; then keytool -importcert -noprompt -alias ca -file /seed/ca.crt -keystore /tmp/truststore.p12 -storetype PKCS12 -storepass fixture; fi; exec zkServer.sh start-foreground /seed/{peer}.cfg"]}
    return services


class ZooKeeperQuorumIntegration(unittest.TestCase):
    def test_tls_three_peer_quorum_restart_and_myid_preservation(self):
        lab = Lab("zookeeper-quorum")
        self.addCleanup(lab.close)
        services = quorum_services(lab)
        lab.compose_path.write_text(json.dumps({"services": services}))
        lab.compose("up", "-d", timeout=600)
        def client(peer, command, check=True):
            return lab.compose("exec", "-T", peer, "env", "CLIENT_JVMFLAGS=" + f"-Dzookeeper.client.secure=true -Dzookeeper.clientCnxnSocket=org.apache.zookeeper.ClientCnxnSocketNetty -Dzookeeper.ssl.keyStore.location=/seed/{peer}.p12 -Dzookeeper.ssl.keyStore.password=fixture -Dzookeeper.ssl.trustStore.location=/tmp/truststore.p12 -Dzookeeper.ssl.trustStore.password=fixture -Dzookeeper.request.timeout=5000", "/bin/bash", "-c", '(sleep 2; printf "%s\\n" "$1"; sleep 3; echo quit) | timeout 20 zkCli.sh -server "$2"', "fixture-client", command, peer + ":2182", check=check, timeout=30)
        for attempt in range(45):
            result = client("z1", "ls /", False)
            if b"[zookeeper]" in result.stdout + result.stderr:
                break
            time.sleep(1)
        else:
            self.fail("3-peer TLS quorum did not become ready; see " + str(lab.folder))
        created = client("z1", "create /acceptance preserved")
        self.assertIn(b"Created /acceptance", created.stdout + created.stderr)
        lab.compose("stop", "z1")
        result = client("z2", "get /acceptance")
        self.assertIn(b"preserved", result.stdout + result.stderr)
        lab.compose("stop", "z2")
        denied = client("z3", "create /no-quorum denied", False)
        self.assertNotIn(b"Created /no-quorum", denied.stdout + denied.stderr)
        lab.compose("up", "-d", timeout=120)
        for attempt in range(45):
            result = client("z3", "get /acceptance", False)
            if b"preserved" in result.stdout + result.stderr:
                break
            time.sleep(1)
        else:
            self.fail("TLS quorum failed recovery")
        lab.compose("restart", timeout=120)
        for attempt in range(45):
            result = client("z2", "get /acceptance", False)
            if b"preserved" in result.stdout + result.stderr:
                break
            time.sleep(1)
        else:
            self.fail("persistent quorum failed full restart")
        for sid in [1,2,3]:
            self.assertEqual((lab.folder / f"data-z{sid}/zookeeper/myid").read_text(), str(sid) + "\n")
