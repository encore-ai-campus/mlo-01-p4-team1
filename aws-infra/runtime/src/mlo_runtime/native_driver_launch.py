"""Native client-mode Spark driver; secrets remain in a volatile private config."""
import json
import hashlib
import os
from pathlib import Path
import re
import sys


def launch(specification, expected_sha256):
    path = Path(specification)
    if (not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256):
        raise ValueError("native driver accepted specification checksum changed")
    value = json.loads(path.read_text())
    home, java = Path(value["spark_home"]), Path(value["java_home"])
    command = value["command"]
    if command[0] != str(home / "bin/spark-submit"):
        raise ValueError("native driver executable mismatch")
    forbidden = ("--packages", "--repositories", "--ivy", "--jars", "--conf",
                 "--properties-file", "--driver-class-path", "--driver-library-path",
                 "--archives", "--files", "--driver-java-options")
    if any(a in forbidden or a.startswith(tuple(p + "=" for p in forbidden)) for a in command):
        raise ValueError("native driver may not override pinned Spark security/dependencies")
    for field in ("app", "launcher", "python_executable"):
        checked = Path(value[field])
        if (checked.is_symlink() or not checked.is_file()
                or checked.stat().st_mode & 0o022
                or hashlib.sha256(checked.read_bytes()).hexdigest() != value[field + "_sha256"]):
            raise ValueError("native driver accepted application/python binary changed before exec")
    dependencies = value.get("application_artifacts", [])
    if not isinstance(dependencies, list):
        raise ValueError("native driver application dependencies invalid")
    approved = set()
    for dep in dependencies:
        if not isinstance(dep, dict) or set(dep) != {"path", "sha256"}:
            raise ValueError("native driver application dependency schema")
        name, expected = dep["path"], dep["sha256"]
        if (not isinstance(name, str) or not name.startswith("/opt/mlo/current/apps/")
                or ".." in Path(name).parts or Path(name).suffix not in {".zip", ".whl", ".py"}
                or not re.fullmatch(r"[a-f0-9]{64}", expected) or name in approved):
            raise ValueError("native driver application artifact path")
        approved.add(name)
        actual = Path(name)
        if (not actual.is_file() or actual.is_symlink()
                or hashlib.sha256(actual.read_bytes()).hexdigest() != expected):
            raise ValueError("native driver application artifact changed before exec")
    marks = [i for i, arg in enumerate(command) if arg == "--py-files"]
    if len(marks) > 1 or any(arg.startswith("--py-files=") for arg in command):
        raise ValueError("native driver application dependency arguments")
    listed = command[marks[0]+1].split(",") if marks and marks[0]+1 < len(command) else []
    if len(listed) != len(approved) or set(listed) != approved:
        raise ValueError("native driver unreviewed Spark python dependency")
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    runtime.chmod(0o700)
    conf = runtime / "conf"
    conf.mkdir(mode=0o700, exist_ok=True)
    # systemd LoadCredential can make root-owned 0600 defaults readable to User=mlo
    # without granting application users direct access to the protected source.
    default_bytes = (credentials / "driver-defaults").read_bytes()
    if hashlib.sha256(default_bytes).hexdigest() != value["spark_defaults_sha256"]:
        raise ValueError("native driver root-owned defaults changed in credential transfer")
    text = default_bytes.decode("utf-8").replace("/run/mlo/secrets", str(credentials))
    properties = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, item = line.split(None, 1)
            if key in properties:
                raise ValueError("duplicate native driver Spark property")
            properties[key] = item
    cp = value["connector_classpath"]
    if (properties.get("spark.driver.extraClassPath") != cp
            or properties.get("spark.executor.extraClassPath") != cp
            or properties.get("spark.authenticate") != "true"
            or properties.get("spark.ssl.enabled") != "true"
            or properties.get("spark.ssl.rpc.enabled") != "true"
            or properties.get("spark.network.crypto.saslFallback") != "false"
            or not properties.get("spark.authenticate.secret.file")
            or {"spark.jars.packages", "spark.jars.repositories", "spark.jars.ivy"} & set(properties)):
        raise ValueError("native driver TLS/connector contract mismatch")
    for key, filename in (("spark.ssl.keyStorePassword", "keystore-password"), ("spark.ssl.keyPassword", "keystore-password"), ("spark.ssl.trustStorePassword", "truststore-password")):
        password = (credentials / filename).read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password):
            raise ValueError("native driver PKI password format")
        text += key + " " + password + "\n"
    target = conf / "spark-defaults.conf"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)
    # Spark's Python driver and executors must use the same accepted CPython
    # release. Otherwise Django/psycopg can silently fall back to /usr/bin/python3.
    py = str(Path(value["python_executable"]))
    os.environ.update({"JAVA_HOME": str(java), "SPARK_HOME": str(home),
                       "SPARK_CONF_DIR": str(conf),
                       "PYSPARK_PYTHON": py, "PYSPARK_DRIVER_PYTHON": py})
    os.execv(command[0], command)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise ValueError("native driver specification and accepted digest required")
    launch(sys.argv[1], sys.argv[2])
