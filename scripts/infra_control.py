#!/usr/bin/env python3
"""Offline preparation by default; live effects require separate signed inputs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "aws-infra/contracts"),
    str(ROOT / "aws-infra/foundation/scripts"),
    str(ROOT / "scripts"),
]
from infra_common.coordination import require, scope_key
from infra_common.preauth_execution import (
    ExecutionGate,
    read_private,
    source_hash,
    validate_review,
    deploy_runtime,
    apply_saved_plan,
)


def scope(binding):
    return {
        "project": "mlo-p4",
        "environment": binding["environment"],
        "generation": binding["generation"],
        "account_ids": binding["account_ids"],
        "state_keys": {
            r: f"dev/{r}/terraform.tfstate"
            for r in [
                "bootstrap",
                "connectivity",
                "encore1",
                "encore2",
                "encore3",
                "encore4",
                "encore5",
            ]
        },
    }


def prepare(binding_path, inventory_path, output):
    from model import validate_binding
    from check_preparation import read_json, validate_inventory

    binding = validate_binding(read_json(binding_path))
    inventory = validate_inventory(read_json(inventory_path))
    require(
        inventory["environment"] == binding["environment"]
        and inventory["generation"] == binding["generation"]
        and inventory["example_only"] == binding["example_only"],
        "inventory/binding generation mismatch",
    )
    output = Path(output).absolute()
    require(
        ".private" in output.parts and not output.exists(),
        "new private preparation folder required",
    )
    output.mkdir(parents=True, mode=0o700)
    physical = scope(binding)
    key = scope_key(physical)
    shared = {
        "schema_version": "infra-coordination-binding-v1",
        "accepted": False,
        "backend": "s3-cas",
        "bucket": binding["state_bucket"],
        "key": "operations/" + key + ".json",
        "scope_key": key,
        "generation": binding["generation"],
    }
    values = {
        "controller-coordination.json": {
            **shared,
            "credential_source": "aws-cli-profile",
            "controller_role_arn": f'arn:aws:iam::{binding["account_ids"]["encore1"]}:role/p4-operation-controller',
        }
    }
    gateways = [
        "http://" + n["private_ip"] + ":3128"
        for n in inventory["nodes"]
        if n["role"] == "gateway"
    ]
    for node in inventory["nodes"]:
        values["hosts/" + node["name"] + "/coordination.json"] = {
            **shared,
            "credential_source": "ec2-instance-profile",
            "node": node["name"],
            "account_alias": node["account"],
            "instance_role_name": "p4-runtime-" + node["name"],
            "sts_route": (
                {"mode": "regional-direct"}
                if node["role"] == "gateway"
                else {"mode": "gateway-connect", "gateway_addresses": gateways}
            ),
        }
        values["hosts/" + node["name"] + "/host-binding-fragment.json"] = {
            "coordination_contract": "/etc/mlo/.private/coordination.json",
            "coordination_scope": physical,
        }
    values["bootstrap-coordination.json"] = {
        "schema_version": "infra-coordination-binding-v1",
        "accepted": False,
        "backend": "single-bootstrap-controller",
        "scope_key": key,
        "generation": binding["generation"],
        "controller_hostname": "SET_AFTER_REVIEW",
        "single_controller_approved": False,
        "expires_at": 0,
    }
    values["scope.json"] = physical
    values["handover.json"] = {
        "schema_version": "preauthentication-handover-v1",
        "ready_for_authentication": False,
        "ready_for_aws": False,
        "accepted": False,
        "example_only": binding["example_only"],
        "source_sha256": source_hash(),
        "next": "verify actual accounts/IAM/AMI/AZ/budget and sign operational bindings; do not auto-accept templates",
    }
    for relative, value in values.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
    return {
        "status": "prepared-unaccepted",
        "files": len(values),
        "host_contracts": 16,
        "ready_for_aws": False,
        "aws_calls": False,
    }


def runtime_clients(review, private_dir):
    import boto3
    import botocore.session
    from botocore.config import Config
    from credential_bridge import create_isolated_config

    env, _, _ = create_isolated_config(
        Path(private_dir) / "deployment-sdk",
        review["account_ids"],
        "aws",
        original=os.environ.copy(),
    )

    def clients(account):
        session = botocore.session.Session()
        session.set_config_variable("config_file", env["AWS_CONFIG_FILE"])
        session.set_config_variable(
            "credentials_file", env["AWS_SHARED_CREDENTIALS_FILE"]
        )
        session.set_config_variable("profile", "p4-controller-" + account)
        sdk = boto3.Session(botocore_session=session)
        config = Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
        )
        return (
            sdk.client(
                "sts",
                region_name="ap-northeast-2",
                endpoint_url="https://sts.ap-northeast-2.amazonaws.com",
                config=config,
            ),
            sdk.client("ssm", region_name="ap-northeast-2", config=config),
            sdk.client("s3", region_name="ap-northeast-2", config=config),
        )

    return clients


def execute(gate, receipt_path):
    from infra_common.factory import load_guard

    review = gate.check()
    p = review["payload"]
    if review["operation"] == "runtime":
        factory = runtime_clients(review, Path(receipt_path).parent)
        owned = []

        def clients(account):
            values = factory(account)
            owned.extend(values)
            return values

        try:
            return deploy_runtime(gate, clients, receipt_path)
        finally:
            for client in owned:
                client.close()
    contract = Path(p["coordination_contract"])
    read_private(contract)
    require(
        hashlib.sha256(contract.read_bytes()).hexdigest()
        == p["coordination_contract_sha256"],
        "coordination input drifted",
    )
    require(
        set(p) >= {"coordination_contract", "coordination_scope"}
        or review["operation"] == "foundation",
        "coordination contract/scope required",
    )
    if review["operation"] == "foundation":
        from model import validate_binding
        from run_foundation import execute as foundation_execute, STAGES

        require(
            set(p)
            == {
                "stage",
                "binding",
                "binding_sha256",
                "private_dir",
                "coordination_contract",
                "coordination_contract_sha256",
                "reviewed_plans",
            },
            "foundation payload fields mismatch",
        )
        binding_path = Path(p["binding"])
        binding = validate_binding(read_private(binding_path), real=True)
        require(
            hashlib.sha256(binding_path.read_bytes()).hexdigest() == p["binding_sha256"]
            and binding["account_ids"] == review["account_ids"]
            and binding["generation"] == review["generation"],
            "foundation inputs drifted",
        )
        require(p["stage"] in STAGES, "invalid foundation stage")
        expected = {
            "bootstrap": ["bootstrap"],
            "apply": ["encore1", "encore2", "encore3", "encore4", "encore5"],
            "connectivity-apply": ["connectivity"],
        }.get(p["stage"], [])
        require(set(p["reviewed_plans"]) == set(expected), "reviewed plan set mismatch")

        def boundary():
            gate.check()
            require(
                hashlib.sha256(contract.read_bytes()).hexdigest()
                == p["coordination_contract_sha256"],
                "coordination input replaced",
            )
            for name, digest in p["reviewed_plans"].items():
                from infra_common.preauth_execution import private_file

                plan = private_file(Path(p["private_dir"]) / (name + ".tfplan"))
                require(
                    hashlib.sha256(plan.read_bytes()).hexdigest() == digest,
                    "externally approved saved plan changed",
                )

        boundary()
        if p["stage"] in {"preflight", "bootstrap-plan"}:
            foundation_execute(
                p["stage"],
                binding_path,
                p["private_dir"],
                enabled=True,
                external_check=boundary,
            )
        else:
            guard = load_guard(
                p["coordination_contract"],
                scope(binding),
                "reviewed-foundation",
                "create",
            )
            with guard.hold():
                foundation_execute(
                    p["stage"],
                    binding_path,
                    p["private_dir"],
                    enabled=True,
                    operation_guard=guard,
                    external_check=boundary,
                )
        return {"status": "complete", "stage": p["stage"], "ready_for_aws": False}
    if review["operation"] == "coordination-init":
        require(
            set(p)
            == {
                "coordination_contract",
                "coordination_scope",
                "coordination_contract_sha256",
            }
            and p["coordination_scope"]["account_ids"] == review["account_ids"]
            and p["coordination_scope"]["generation"] == review["generation"],
            "operation initialization scope mismatch",
        )
        guard = load_guard(
            p["coordination_contract"],
            p["coordination_scope"],
            "reviewed-bootstrap-initialization",
            "create",
        )
        gate.check()
        guard.initialize()
        return {"status": "initialized", "ready_for_aws": False}
    require(
        review["operation"] == "destroy"
        and p["coordination_scope"]["account_ids"] == review["account_ids"]
        and p["coordination_scope"]["generation"] == review["generation"],
        "destroy coordination target mismatch",
    )
    guard = load_guard(
        p["coordination_contract"],
        p["coordination_scope"],
        "reviewed-retention-executor",
        "destroy",
    )
    with guard.hold():
        return apply_saved_plan(gate, guard=guard, receipt_path=receipt_path)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print("FAIL: invalid infra control arguments", file=sys.stderr)
        raise SystemExit(2)


def main():
    parser = Parser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["prepare", "validate", "execute", "credential-process", "oidc-config"],
    )
    for name in [
        "binding",
        "inventory",
        "output",
        "review",
        "approval",
        "authority-key",
        "receipt",
        "account",
        "role",
        "repository",
        "environment",
        "purpose",
    ]:
        parser.add_argument("--" + name)
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args()
    try:
        if args.operation == "credential-process":
            from infra_common.oidc import credentials

            result = credentials(
                args.account, args.role, args.repository, args.environment
            )
        elif args.operation == "oidc-config":
            from infra_common.oidc import write_config

            review = validate_review(read_private(args.review))
            write_config(
                args.output,
                review["account_ids"],
                args.purpose,
                args.repository,
                args.environment,
            )
            result = {
                "status": "credential-process-configured",
                "tokens_persisted": False,
                "aws_calls": False,
            }
        elif args.operation == "prepare":
            result = prepare(args.binding, args.inventory, args.output)
        elif args.operation == "validate":
            review = validate_review(read_private(args.review))
            require(
                review["source_sha256"] == source_hash(),
                "review source digest mismatch",
            )
            result = {
                "status": "review-envelope-valid",
                "accepted": review["accepted"],
                "payload_execution_checks": "deferred to guarded execution",
                "ready_for_aws": False,
                "aws_calls": False,
            }
        else:
            require(args.enable_live, "live execution is disabled by default")
            gate = ExecutionGate(
                args.review, args.approval, args.authority_key, enabled=args.enable_live
            )
            result = execute(gate, args.receipt)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        # Never echo credentials, raw SDK exception responses, CLI output or argv.
        print(
            "FAIL: infra control refused/failed; inspect protected inputs and receipt",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
