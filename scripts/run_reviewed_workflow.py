#!/usr/bin/env python3
"""Manual protected-runner entrypoint. No authentication before signed input checks."""

import os
import hashlib
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "aws-infra/contracts")]
from infra_common.coordination import require
from infra_common.preauth_execution import ExecutionGate, sha, private_file
from infra_common.oidc import ENVIRONMENTS, OIDC_PHASES, write_config
from infra_control import execute


def target(review, purpose):
    stages = {
        "plan": {"preflight", "plan", "connectivity-plan"},
        "apply": {"apply", "connectivity-apply", "export", "verify"},
    }
    if purpose in stages:
        require(
            review["operation"] == "foundation"
            and review["payload"]["stage"] in stages[purpose],
            "workflow foundation stage mismatch",
        )
    else:
        require(
            purpose in {"runtime", "destroy"} and review["operation"] == purpose,
            "workflow operation mismatch",
        )


def authority_key(path, expected):
    require(
        isinstance(expected, str) and re.fullmatch(r"[a-f0-9]{64}", expected),
        "separately pinned authority fingerprint required",
    )
    path = private_file(path)
    require(
        hashlib.sha256(path.read_bytes()).hexdigest() == expected,
        "approval authority key mismatch",
    )
    return path


def web_release(review, purpose):
    if os.environ.get("MLO_WEB_DEPLOY") != "true":
        return None
    require(
        purpose == "runtime" and os.environ.get("GITHUB_EVENT_NAME") == "push",
        "main web deployment requires a push event",
    )
    commit = os.environ.get("GITHUB_SHA", "")
    image = os.environ.get("MLO_WEB_IMAGE", "")
    repository = review["account_ids"]["encore1"] + ".dkr.ecr.ap-northeast-2.amazonaws.com/mlo-p4/api"
    require(
        re.fullmatch(r"[a-f0-9]{40}", commit)
        and subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == commit
        and re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", image),
        "exact main checkout and published web digest required",
    )
    return "main-" + commit, image


def credential_failure_phase(error):
    from botocore.exceptions import CredentialRetrievalError

    if not isinstance(error, CredentialRetrievalError):
        return None
    marker = error.kwargs.get("error_msg")
    for phase in OIDC_PHASES:
        if marker == f"MLO_OIDC_PHASE={phase}\n":
            return phase
    return "credential-process"


def main():
    phase = "signed-inputs"
    try:
        purpose = os.environ.get("MLO_PURPOSE")
        review_id = os.environ.get("MLO_REVIEW_SHA256", "")
        require(
            re.fullmatch(r"[a-f0-9]{64}", review_id), "exact reviewed digest required"
        )
        require(
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("MLO_REPOSITORY_PRIVATE") == "true",
            "private main workflow required",
        )
        require(
            platform.system() == "Linux"
            and platform.machine() == "x86_64"
            and sys.version_info[:3] == (3, 14, 8),
            "prepared Linux X64 Python runner required",
        )
        folder = Path(os.environ["MLO_REVIEW_ROOT"]).absolute()
        require(
            ".private" in folder.parts
            and not folder.is_symlink()
            and not folder.resolve().is_relative_to(ROOT)
            and folder.is_dir(),
            "external protected review root required",
        )
        folder = folder / review_id
        require(
            not folder.is_symlink()
            and folder.is_dir()
            and not folder.stat().st_mode & 0o077,
            "protected review folder required",
        )
        key = authority_key(
            folder / "authority-public.key", os.environ.get("MLO_APPROVAL_KEY_SHA256")
        )
        gate = ExecutionGate(
            folder / "review.json",
            folder / "approval.json",
            key,
            enabled=True,
        )
        review = gate.check()
        require(sha(review) == review_id, "review directory/digest mismatch")
        target(review, purpose)
        expected_web_release = web_release(review, purpose)
        # Config holds commands only; web identity and session keys remain in
        # credential_process memory. GitHub Environment approval is separate.
        credential_purpose = "apply" if purpose == "destroy" else purpose
        if expected_web_release is not None:
            credential_purpose = "web-deploy"
        phase = "credential-config"
        config = write_config(
            folder / "oidc-config",
            review["account_ids"],
            credential_purpose,
            os.environ["GITHUB_REPOSITORY"],
            ENVIRONMENTS[credential_purpose],
        )
        for name in list(os.environ):
            if name.startswith("AWS_"):
                del os.environ[name]
        os.environ.update(
            AWS_CONFIG_FILE=str(config),
            AWS_SHARED_CREDENTIALS_FILE=os.devnull,
            AWS_EC2_METADATA_DISABLED="true",
            AWS_PAGER="",
            AWS_CLI_AUTO_PROMPT="off",
            AWS_STS_REGIONAL_ENDPOINTS="regional",
        )
        phase = "operation"
        execute(gate, folder / "execution-receipt.json", web_release=expected_web_release)
        print("PASS: reviewed manual operation completed; inspect private receipt")
        return 0
    except Exception as error:
        if phase == "operation":
            oidc_phase = credential_failure_phase(error)
            if oidc_phase is not None:
                phase = f"oidc/{oidc_phase}"
        print(
            f"FAIL: manual workflow stopped at {phase}; inspect protected inputs and receipt",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
