"""Run advertising regressions against a new loopback-only disposable MongoDB.

Uses an installed mongod and the current Python environment; installs nothing.
The process, replica set directory and UUID test databases belong to this run.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND.parents[1]


def main():
    executable = os.environ.get("ADS_TEST_MONGOD") or shutil.which("mongod")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("An installed mongod is required; no software is installed.")
    version = subprocess.check_output([executable, "--version"], text=True).splitlines()[0]
    folder = APP / ".local" / ("ads-mongo-regression-" + uuid4().hex)
    folder.mkdir(parents=True, mode=0o700)
    data = folder / "data"
    data.mkdir(mode=0o700)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    uri = f"mongodb://127.0.0.1:{port}/?replicaSet=ads-rs&directConnection=true"
    process = subprocess.Popen(
        [
            executable,
            "--bind_ip",
            "127.0.0.1",
            "--port",
            str(port),
            "--dbpath",
            str(data),
            "--replSet",
            "ads-rs",
            "--wiredTigerCacheSizeGB",
            "0.25",
            "--logpath",
            str(folder / "mongod.log"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    client = None
    result = {
        "mongo_version": version,
        "mode": "ISOLATED_LOOPBACK_MONGODB",
        "external_services_contacted": False,
        "mongo_port": port,
        "test_failures": None,
        "process_stopped": False,
        "data_removed": False,
    }
    try:
        from pymongo import MongoClient

        client = MongoClient(
            f"mongodb://127.0.0.1:{port}/?directConnection=true",
            serverSelectionTimeoutMS=500,
            connectTimeoutMS=500,
            socketTimeoutMS=1000,
        )
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise RuntimeError("The isolated MongoDB process exited; inspect its local log.")
            try:
                client.admin.command("ping")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "The isolated MongoDB process did not become ready."
                    ) from None
                time.sleep(0.1)
        client.admin.command(
            {
                "replSetInitiate": {
                    "_id": "ads-rs",
                    "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}],
                },
            }
        )
        while not client.admin.command("hello").get("isWritablePrimary"):
            if time.monotonic() >= deadline:
                raise RuntimeError("The isolated replica set did not elect a primary.")
            time.sleep(0.1)

        os.environ.update(
            {
                "DJANGO_SETTINGS_MODULE": "config.settings",
                "DJANGO_SECRET_KEY": "synthetic-local-mongo-regression-not-production",
                "DJANGO_DEBUG": "1",
                "ADS_RUN_MONGO_TESTS": "1",
                "ADS_TEST_MONGODB_URI": uri,
            }
        )
        sys.path.insert(0, str(BACKEND))
        import django

        django.setup()
        from django.test.runner import DiscoverRunner

        connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

        def allowed(address):
            return (
                isinstance(address, tuple)
                and len(address) >= 2
                and address[0] in {"127.0.0.1", "::1", "localhost"}
                and address[1] == port
            )

        def guarded_connect(sock, address):
            if not allowed(address):
                raise AssertionError("Only this run's isolated MongoDB connection is permitted.")
            return connect(sock, address)

        def guarded_connect_ex(sock, address):
            if not allowed(address):
                raise AssertionError("Only this run's isolated MongoDB connection is permitted.")
            return connect_ex(sock, address)

        with (
            patch.object(socket.socket, "connect", guarded_connect),
            patch.object(socket.socket, "connect_ex", guarded_connect_ex),
            patch("subprocess.Popen", side_effect=AssertionError("Unreviewed child process")),
        ):
            failures = DiscoverRunner(verbosity=1, interactive=False).run_tests(["tests"])
        result["test_failures"] = failures
        # Every real integration class validates its UUID database name before
        # deletion. Refuse to silently leave uncleaned databases on this process.
        remaining = [
            name for name in client.list_database_names() if name.startswith("ads_integration_")
        ]
        result["remaining_test_databases"] = len(remaining)
        if remaining:
            raise RuntimeError("An integration test did not clean up its UUID database.")
        return int(bool(failures))
    finally:
        if client is not None:
            try:
                client.admin.command({"shutdown": 1, "timeoutSecs": 5})
            except Exception:
                pass
            client.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        result["process_stopped"] = process.poll() is not None
        # Resolve and check the exact newly-created directory before recursion.
        resolved = data.resolve()
        if resolved.parent != folder.resolve() or resolved.name != "data":
            raise RuntimeError("Refusing cleanup outside this run's data directory.")
        shutil.rmtree(resolved)
        result["data_removed"] = not data.exists()
        (folder / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {"result": result, "evidence": str(folder / "result.json")}, ensure_ascii=False
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
