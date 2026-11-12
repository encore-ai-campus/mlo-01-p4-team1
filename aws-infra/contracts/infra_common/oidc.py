"""GitHub OIDC credential_process. Tokens exist only in memory and the SDK pipe."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time
import urllib.parse
import urllib.request

from .coordination import require

ENVIRONMENTS = {
    "plan": "infra-plan",
    "apply": "infra-apply",
    "runtime": "infra-runtime",
    "web-deploy": "infra-runtime",
}

OIDC_PHASES = (
    "context", "token-request", "claims", "claims-issuer-audience", "claims-scope",
    "claims-time", "sts-exchange", "session",
)
MAX_ISSUANCE_WAIT = 5


class OIDCPhaseError(ValueError):
    def __init__(self, phase):
        self.phase = phase
        super().__init__("OIDC credential process failed")


@contextmanager
def oidc_phase(phase):
    try:
        yield
    except OIDCPhaseError:
        raise
    except Exception:
        raise OIDCPhaseError(phase) from None


def validate_claims(
    token,
    *,
    account,
    role,
    repository,
    environment,
    now=None,
    repository_id=None,
    owner_id=None,
    clock=time.time,
    pause=None,
):
    """AWS verifies the JWT signature; reject unexpected local claims first."""
    require(
        re.fullmatch(r"[0-9]{12}", account)
        and re.fullmatch(
            r"arn:aws:iam::" + account + r":role/p4-ci-(plan|apply|runtime|web-deploy)", role
        ),
        "exact CI role required",
    )
    require(
        environment == ENVIRONMENTS[role.split(":role/p4-ci-", 1)[1]],
        "CI role/environment mismatch",
    )
    name = repository
    if repository_id is not None or owner_id is not None:
        require(
            re.fullmatch(r"[1-9][0-9]*", str(repository_id))
            and re.fullmatch(r"[1-9][0-9]*", str(owner_id)),
            "immutable repository IDs required",
        )
        owner, repo = repository.split("/", 1)
        name = owner + "@" + str(owner_id) + "/" + repo + "@" + str(repository_id)
    parts = token.split(".")
    require(len(parts) == 3, "OIDC token malformed")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    with oidc_phase("claims-issuer-audience"):
        require(claims.get("iss") == "https://token.actions.githubusercontent.com"
                and claims.get("aud") == "sts.amazonaws.com", "OIDC issuer/audience mismatch")
    with oidc_phase("claims-scope"):
        require(
            claims.get("sub") == "repo:" + name + ":environment:" + environment
            and claims.get("repository") == repository
            and claims.get("ref") == "refs/heads/main",
            "OIDC target mismatch",
        )
        if repository_id is not None:
            require(
                claims.get("repository_id") == str(repository_id)
                and claims.get("repository_owner_id") == str(owner_id),
                "OIDC immutable identity mismatch",
            )
    with oidc_phase("claims-time"):
        issued, expires = claims.get("iat"), claims.get("exp")
        require(all(type(value) in (int, float) and math.isfinite(value) for value in (issued, expires)),
                "OIDC time claims malformed")
        current = clock() if now is None else now
        # Wait through small runner clock skew, then recheck the original strict
        # window. AWS still validates the signed token and role trust itself.
        delay = issued - current
        if now is None and pause is not None and 0 < delay <= MAX_ISSUANCE_WAIT and issued < expires:
            pause(delay)
            current = clock()
        require(issued <= current < expires, "OIDC token not currently valid")
    return claims


def request_context(repository):
    require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_REPOSITORY") == repository,
        "trusted main workflow context required",
    )
    repository_id = os.environ.get("GITHUB_REPOSITORY_ID", "")
    owner_id = os.environ.get("GITHUB_REPOSITORY_OWNER_ID", "")
    require(
        re.fullmatch(r"[1-9][0-9]*", repository_id)
        and re.fullmatch(r"[1-9][0-9]*", owner_id),
        "trusted immutable repository context required",
    )
    endpoint = urllib.parse.urlsplit(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""))
    require(
        endpoint.scheme == "https"
        and endpoint.hostname
        and endpoint.hostname.endswith(".actions.githubusercontent.com")
        and endpoint.port in (None, 443)
        and endpoint.username is None
        and endpoint.password is None
        and not endpoint.fragment,
        "untrusted OIDC request endpoint",
    )
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    require(isinstance(bearer, str) and bearer, "GitHub id-token permission required")
    query = urllib.parse.parse_qs(endpoint.query)
    query["audience"] = ["sts.amazonaws.com"]
    url = urllib.parse.urlunsplit(
        endpoint._replace(query=urllib.parse.urlencode(query, doseq=True))
    )
    return url, bearer, repository_id, owner_id


def credentials(account, role, repository, environment, *, request=None, sts=None):
    with oidc_phase("context"):
        url, bearer, repository_id, owner_id = request_context(repository)
    if request is None:

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("OIDC redirect refused")

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

        def request(url, headers):
            with opener.open(
                urllib.request.Request(url, headers=headers), timeout=5
            ) as response:
                raw = response.read(65537)
            require(len(raw) <= 65536, "OIDC response too large")
            return json.loads(raw)

    with oidc_phase("token-request"):
        token = request(url, {"Authorization": "Bearer " + bearer})["value"]
    with oidc_phase("claims"):
        validate_claims(
            token,
            account=account,
            role=role,
            repository=repository,
            environment=environment,
            repository_id=repository_id,
            owner_id=owner_id,
            pause=time.sleep,
        )
    with oidc_phase("sts-exchange"):
        result = assume_session(sts, role, token)
    with oidc_phase("session"):
        return format_session(result)


def assume_session(sts, role, token):
    owned = sts is None
    if owned:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config

        sts = boto3.client(
            "sts",
            region_name="ap-northeast-2",
            endpoint_url="https://sts.ap-northeast-2.amazonaws.com",
            config=Config(
                signature_version=UNSIGNED,
                connect_timeout=3,
                read_timeout=5,
                retries={"total_max_attempts": 1, "mode": "standard"},
                proxies={},
            ),
        )
    try:
        return sts.assume_role_with_web_identity(
            RoleArn=role,
            RoleSessionName="mlo-p4-" + str(os.getpid()),
            WebIdentityToken=token,
            DurationSeconds=900,
        )["Credentials"]
    finally:
        if owned:
            sts.close()


def format_session(result):
    expires = result["Expiration"]
    require(
        isinstance(expires, datetime)
        and expires.tzinfo is not None
        and expires.timestamp() > time.time(),
        "STS credentials expired",
    )
    return {
        "Version": 1,
        "AccessKeyId": result["AccessKeyId"],
        "SecretAccessKey": result["SecretAccessKey"],
        "SessionToken": result["SessionToken"],
        "Expiration": expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_config(path, account_ids, purpose, repository, environment):
    require(
        purpose in ENVIRONMENTS
        and re.fullmatch(r"[\w.-]+/[\w.-]+", repository)
        and environment == ENVIRONMENTS[purpose],
        "CI scope mismatch",
    )
    require(
        len(set(account_ids.values())) == 5
        and set(account_ids) == {"encore1", "encore2", "encore3", "encore4", "encore5"},
        "five OIDC target accounts required",
    )
    path = Path(path).absolute()
    require(
        ".private" in path.parts and not path.is_symlink() and path.parent.is_dir(),
        "private credential config required",
    )
    launcher = Path(__file__).resolve().parents[3] / "scripts/infra_control.py"
    text = ""
    profiles = {"encore1": account_ids["encore1"]} if purpose == "web-deploy" else account_ids
    for alias, account in sorted(profiles.items()):
        require(
            isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account),
            "invalid OIDC account",
        )
        args = [
            sys.executable,
            str(launcher),
            "credential-process",
            "--account",
            account,
            "--role",
            f"arn:aws:iam::{account}:role/p4-ci-{purpose}",
            "--repository",
            repository,
            "--environment",
            environment,
        ]
        text += f"[profile {alias}]\nregion = ap-northeast-2\nsts_regional_endpoints = regional\ncredential_process = {shlex.join(args)}\n\n"
    raw = text.encode()
    if path.exists():
        require(
            path.is_file()
            and not path.stat().st_mode & 0o077
            and path.read_bytes() == raw,
            "refuse to replace credential config",
        )
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return path
