"""Explicit accepted private coordination inputs; no clients at import time."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time

from .coordination import OperationLease, S3CAS, require, scope_key


class BootstrapGuard:
    """One named controller, before the state bucket exists. Not a distributed lock.

    A different machine is forbidden by the accepted bootstrap contract. A crash
    leaves a pending receipt; deleting it is not an automated recovery mechanism.
    """

    operation = "create"

    def __init__(self, folder, scope, expires_at):
        self.folder, self.scope, self.expires_at = Path(folder), scope, expires_at
        self.owned = False

    def check(self):
        require(
            self.owned and time.time() < self.expires_at,
            "bootstrap controller lock missing/expired",
        )
        return {
            "scope": self.scope,
            "epoch": 0,
            "operation": "create",
            "backend": "single-bootstrap-controller",
        }

    @contextmanager
    def hold(self):
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.folder / "bootstrap-coordination.json"
        with (self.folder / "bootstrap-coordination.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            require(
                not path.exists(),
                "bootstrap already attempted; inspect receipt before any retry",
            )

            def write(state, *, exclusive=False):
                target = path if exclusive else path.with_suffix(".complete.tmp")
                fd = os.open(
                    target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as stream:
                    json.dump({"scope": self.scope, "state": state}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if not exclusive:
                    os.replace(target, path)
                directory = os.open(self.folder, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

            write("pending", exclusive=True)
            self.owned = True
            try:
                self.check()
                yield self
                self.check()
                write("complete")
            finally:
                self.owned = False
                fcntl.flock(lock, fcntl.LOCK_UN)


def load_guard(path, scope, owner, operation, *, client=None):
    path = Path(path).absolute()
    require(not path.is_symlink(), "symlink coordination contract refused")
    path = path.resolve()
    require(
        ".private" in path.parts and path.is_file() and not path.stat().st_mode & 0o077,
        "coordination contract must be protected and private",
    )
    spec = json.loads(path.read_text())
    require(
        spec["schema_version"] == "infra-coordination-binding-v1"
        and spec["accepted"] is True
        and spec["scope_key"] == scope_key(scope)
        and spec["generation"] == scope["generation"],
        "coordination contract unaccepted/mismatched",
    )
    if spec["backend"] == "single-bootstrap-controller":
        require(
            operation == "create"
            and spec["controller_hostname"] == __import__("socket").gethostname()
            and spec["single_controller_approved"] is True
            and time.time() < spec["expires_at"],
            "bootstrap must have one approved controller and deadline",
        )
        return BootstrapGuard(path.parent, scope, spec["expires_at"])
    require(
        spec["backend"] == "s3-cas"
        and spec["key"] == "operations/" + scope_key(scope) + ".json",
        "operation lock key mismatch",
    )
    require(
        isinstance(spec["bucket"], str)
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", spec["bucket"]),
        "invalid operation bucket",
    )
    if client is None:
        import boto3
        import botocore.session
        from botocore.config import Config

        session = botocore.session.Session()
        mode = spec.get("credential_source")
        require(
            mode in {"aws-cli-profile", "ec2-instance-profile"},
            "explicit credential source required",
        )
        if mode == "aws-cli-profile":
            # Only the local operator needs the CLI login bridge. Host artifacts
            # explicitly select EC2 credentials and do not depend on Foundation.
            root = Path(__file__).resolve().parents[3]
            sys.path.insert(0, str(root / "aws-infra/foundation/scripts"))
            from credential_bridge import create_isolated_config

            env, _, _ = create_isolated_config(
                path.parent / "coordination-sdk",
                scope["account_ids"],
                "aws",
                original=os.environ.copy(),
            )
            session.set_config_variable("config_file", env["AWS_CONFIG_FILE"])
            session.set_config_variable(
                "credentials_file", env["AWS_SHARED_CREDENTIALS_FILE"]
            )
            session.set_config_variable("profile", "encore1")
            require(spec.get("controller_role_arn") == "arn:aws:iam::" + scope["account_ids"]["encore1"] + ":role/p4-operation-controller", "exact operation controller role required")
            # Dedicated operation role has no Terraform state access. The CLI
            # source needs only the separately reviewed AssumeRole permission.
            config_path = Path(env["AWS_CONFIG_FILE"])
            with config_path.open("a") as stream:
                stream.write("\n[profile p4-operation-controller]\nsource_profile = encore1\nrole_arn = " + spec["controller_role_arn"] + "\nregion = ap-northeast-2\nsts_regional_endpoints = regional\n")
            session.set_config_variable("profile", "p4-operation-controller")
        else:
            from .transport import routes
            routes(spec, scope)
            from botocore.credentials import CredentialResolver

            require(
                re.fullmatch(
                    r"[A-Za-z0-9+=,.@_-]{1,64}", spec.get("instance_role_name", "")
                ),
                "accepted instance role required",
            )
            session.set_config_variable("config_file", os.devnull)
            session.set_config_variable("credentials_file", os.devnull)
            from .transport import metadata_provider
            provider = metadata_provider()
            session.register_component(
                "credential_provider", CredentialResolver([provider])
            )
        client = boto3.Session(botocore_session=session).client(
            "s3",
            region_name="ap-northeast-2",
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 1, "mode": "standard"},
                signature_version="s3v4",
                proxies={},
            ),
        )
        if mode == "ec2-instance-profile":
            credentials = session.get_credentials()
            require(credentials is not None, "instance credentials unavailable")
            from .transport import verify_host
            verify_host(session, spec, scope)
    return OperationLease(
        S3CAS(client, spec["bucket"], spec["key"]), scope, owner, operation
    )
