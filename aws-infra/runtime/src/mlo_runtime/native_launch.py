"""Foreground Spark launcher; no daemon shell or runtime dependency download."""
import os
from pathlib import Path
import re
import sys


def launch(component, home, config, arguments):
    if component not in {"master", "worker"}:
        raise ValueError("unknown native Spark component")
    home, config = Path(home), Path(config)
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    password = (credentials / "keystore-password").read_text().strip()
    trust = (credentials / "truststore-password").read_text().strip()
    # Spark's own shell launcher splits *_OPTS. Accept only generated PKI
    # passwords without whitespace, quotes or shell syntax; never print them.
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", trust):
        raise ValueError("native PKI password format")
    properties = {}
    for line in (config / "spark-defaults.conf").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, value = line.split(None, 1)
            if key.startswith(("spark.ssl.", "spark.authenticate", "spark.deploy.", "spark.port.")):
                if any(x.isspace() for x in value) or any(x in value for x in "\"'`$\\"):
                    raise ValueError("unsafe Spark JVM option")
                properties[key] = value
    properties.update({"spark.ssl.keyStorePassword": password, "spark.ssl.keyPassword": password,
                       "spark.ssl.trustStorePassword": trust})
    if component == "master":
        properties.update({
            "zookeeper.client.secure": "true",
            "zookeeper.clientCnxnSocket": "org.apache.zookeeper.ClientCnxnSocketNetty",
            "zookeeper.ssl.keyStore.location": str(credentials / "spark-keystore.p12"),
            "zookeeper.ssl.trustStore.location": str(credentials / "cluster-truststore.p12"),
            "zookeeper.ssl.keyStore.passwordPath": str(credentials / "keystore-password"),
            "zookeeper.ssl.trustStore.passwordPath": str(credentials / "truststore-password"),
        })
    os.environ["SPARK_" + component.upper() + "_OPTS"] = " ".join("-D" + k + "=" + v for k, v in properties.items())
    os.environ["SPARK_CONF_DIR"] = str(config)
    executable = home / "bin/spark-class"
    os.execv(str(executable), [str(executable), "org.apache.spark.deploy." + component + "." + component.title(), *arguments])


if __name__ == "__main__":
    launch(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
