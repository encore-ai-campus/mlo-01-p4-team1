#!/usr/bin/env python3
"""Explicit real-run entrance. Local checks never import or call this entrypoint."""

import argparse, hashlib, json, os, subprocess, sys
from pathlib import Path
from model import *
from credential_bridge import create_isolated_config, export_credentials

sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
from infra_common.coordination import require_operation, CoordinationError
from infra_common.factory import load_guard, BootstrapGuard

STAGES = [
    "preflight",
    "bootstrap-plan",
    "bootstrap",
    "plan",
    "apply",
    "connectivity-plan",
    "connectivity-apply",
    "export",
    "verify",
]


def safe_env(source=None):
    # Profiles may be used only on this explicitly enabled path.
    env = {
        k: v
        for k, v in (os.environ if source is None else source).items()
        if not k.startswith("TF_CLI_ARGS")
        and k
        not in [
            "TF_LOG",
            "TF_LOG_PATH",
            "TF_WORKSPACE",
            "TF_CLI_CONFIG_FILE",
            "TF_DATA_DIR",
        ]
    }
    env.update(TF_WORKSPACE="default", CHECKPOINT_DISABLE="1")
    return env


def command(argv, *, cwd=None, env=None):
    p = subprocess.run(argv, cwd=cwd, env=safe_env(env), capture_output=True, text=True)
    require(p.returncode == 0, "external CLI failed; receipt preserved without secrets")
    return p.stdout


def binding_digest(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def execute(
    stage,
    binding_path,
    private_dir,
    *,
    enabled=False,
    terraform=None,
    aws="aws",
    operation_guard=None,
    external_check=None,
):
    require(enabled, "real execution disabled: --enable-real required")
    binding_path = Path(binding_path).resolve()
    private_dir = Path(private_dir).resolve()
    require(
        ".private" in binding_path.parts and ".private" in private_dir.parts,
        "bindings/receipts require .private",
    )
    b = validate_binding(read_json(binding_path), real=True)
    require(
        stage in STAGES, "unsupported stage; import/destroy intentionally unavailable"
    )
    if stage not in {"preflight", "bootstrap-plan"}:
        try:
            token = require_operation(operation_guard, "create")
            require(
                token["scope"]["environment"] == b["environment"]
                and token["scope"]["generation"] == b["generation"]
                and token["scope"]["account_ids"] == b["account_ids"],
                "common operation scope mismatch",
            )
        except CoordinationError as exc:
            raise ContractError("accepted common operation guard required") from exc
        require(
            stage == "bootstrap" or not isinstance(operation_guard, BootstrapGuard),
            "bootstrap guard cannot protect account/deployment operations",
        )
    terraform = terraform or str(FOUNDATION / ".cache/toolchain/terraform")
    private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = private_dir / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ContractError(
            "another run or stale lock; inspect before manual recovery"
        ) from exc
    os.close(fd)
    try:
        receipt_path = private_dir / "receipt.json"
        digest = binding_digest(b)
        receipt = (
            read_json(receipt_path)
            if receipt_path.exists()
            else {
                "digest": digest,
                "completed": [],
                "pending": None,
                "generation": b["generation"],
            }
        )
        require(
            receipt["digest"] == digest,
            "binding changed; start a new reviewed run directory",
        )
        require(
            receipt["pending"] is None,
            "partial/ambiguous stage; inspect state and reconcile receipt before rerun",
        )
        require(
            stage not in receipt["completed"],
            "already completed; use a new reviewed run for changes",
        )
        index = STAGES.index(stage)
        require(receipt["completed"] == STAGES[:index], "stages must run in order")

        def save():
            temp = receipt_path.with_suffix(".tmp")
            temp.write_text(json.dumps(receipt, indent=2) + "\n")
            os.chmod(temp, 0o600)
            os.replace(temp, receipt_path)

        receipt["pending"] = stage
        save()

        def boundary():
            if external_check is not None:
                external_check()
            if stage not in {"preflight", "bootstrap-plan"}:
                require_operation(operation_guard, "create")

        boundary()
        require(
            json.loads(command([terraform, "version", "-json"]))["terraform_version"]
            == "1.11.4",
            "pinned Terraform required",
        )
        require(
            command([aws, "--version"]).startswith("aws-cli/2.37.10 "),
            "pinned AWS CLI 2.37.10 required",
        )
        source_env = os.environ.copy()
        run_env, original_config, original_credentials = create_isolated_config(
            private_dir / "sdk-config", b["account_ids"], aws, original=source_env
        )
        for account in ACCOUNTS:
            # Validate CLI login sources before Terraform can touch state. Never persist returned credentials.
            export_credentials(
                aws, account, original_config, original_credentials, original=source_env
            )

        def run(argv, *, cwd=None):
            boundary()
            result = command(argv, cwd=cwd, env=run_env)
            boundary()
            return result

        # Every stage rechecks all identities before any Terraform command.
        for a in ACCOUNTS:
            raw = run(
                [aws, "sts", "get-caller-identity", "--profile", a, "--output", "json"]
            )
            require(
                json.loads(raw)["Account"] == b["account_ids"][a],
                "account identity mismatch",
            )
        tfvars = private_dir / "binding.tfvars.json"
        tfvars.write_text(json.dumps({"binding": b}))
        os.chmod(tfvars, 0o600)
        roots = {
            a: FOUNDATION / f"terraform/environments/dev/{a}"
            for a in [*ACCOUNTS, "connectivity"]
        }
        if stage == "bootstrap-plan":
            root = FOUNDATION / "terraform/bootstrap"
            run([terraform, "init", "-input=false"], cwd=root)
            plan = private_dir / "bootstrap.tfplan"
            run(
                [
                    terraform,
                    "plan",
                    "-input=false",
                    "-lock-timeout=60s",
                    "-var-file=" + str(tfvars),
                    "-out=" + str(plan),
                ],
                cwd=root,
            )
            receipt.setdefault("plans", {})["bootstrap"] = hashlib.sha256(plan.read_bytes()).hexdigest()
            save()
        elif stage == "bootstrap":
            root = FOUNDATION / "terraform/bootstrap"
            plan = private_dir / "bootstrap.tfplan"
            require(plan.is_file() and hashlib.sha256(plan.read_bytes()).hexdigest() == receipt["plans"]["bootstrap"], "reviewed bootstrap plan changed/missing")
            run([terraform, "apply", "-input=false", str(plan)], cwd=root)
        elif stage in ["plan", "apply", "connectivity-plan", "connectivity-apply"]:
            # Accounts are created first. Connectivity needs their real outputs, never remote-state data sources.
            network = stage.startswith("connectivity-")
            planning = stage in ["plan", "connectivity-plan"]
            if network:
                account_outputs = {}
                for a in ACCOUNTS:
                    raw = json.loads(run([terraform, "output", "-json"], cwd=roots[a]))
                    value = raw["foundation"]["value"]
                    require(
                        value["component"] == a
                        and value["generation"] == b["generation"]
                        and value["example_only"] is False,
                        "account output provenance mismatch",
                    )
                    account_outputs[a] = value
                network_vars = private_dir / "accounts.tfvars.json"
                network_vars.write_text(json.dumps({"accounts": account_outputs}))
                os.chmod(network_vars, 0o600)
            for a, root in roots.items():
                if (a == "connectivity") != network:
                    continue
                plan = private_dir / (a + ".tfplan")
                if planning:
                    backend = private_dir / (a + ".backend.json")
                    backend.write_text(json.dumps(backend_config(a, b)))
                    os.chmod(backend, 0o600)
                    run(
                        [
                            terraform,
                            "init",
                            "-input=false",
                            "-lockfile=readonly",
                            "-reconfigure",
                            "-backend-config=" + str(backend),
                        ],
                        cwd=root,
                    )
                    args = [
                        terraform,
                        "plan",
                        "-input=false",
                        "-lock-timeout=60s",
                        "-var-file=" + str(tfvars),
                        "-out=" + str(plan),
                    ]
                    if network:
                        args.append("-var-file=" + str(network_vars))
                    run(args, cwd=root)
                    receipt.setdefault("plans", {})[a] = hashlib.sha256(
                        plan.read_bytes()
                    ).hexdigest()
                    save()
                else:
                    require(
                        plan.exists()
                        and hashlib.sha256(plan.read_bytes()).hexdigest()
                        == receipt["plans"][a],
                        "saved plan changed/missing",
                    )
                    run([terraform, "apply", "-input=false", str(plan)], cwd=root)
        elif stage == "export":
            for a, root in roots.items():
                output = run([terraform, "output", "-json"], cwd=root)
                dest = private_dir / (a + ".output.json")
                dest.write_text(output)
                os.chmod(dest, 0o600)
            from export_inventory import export, write_inventory

            write_inventory(
                export([private_dir / (a + ".output.json") for a in roots]),
                private_dir / "inventory.json",
            )
        elif stage == "verify":
            inv = validate_inventory(read_json(private_dir / "inventory.json"))
            require(
                inv["generation"] == b["generation"] and inv["example_only"] is False,
                "verify metadata mismatch",
            )
            # Only artifact/shape verification; actual traffic checks are a separate operator checklist.
        receipt["completed"].append(stage)
        receipt["pending"] = None
        save()
    finally:
        lock.unlink()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--binding", type=Path, required=True)
    p.add_argument("--private-dir", type=Path, required=True)
    p.add_argument("--enable-real", action="store_true")
    p.add_argument("--coordination-contract", type=Path)
    a = p.parse_args()
    try:
        require(a.enable_real, "real execution disabled")
        if a.stage in {"preflight", "bootstrap-plan"}:
            execute(a.stage, a.binding, a.private_dir, enabled=True)
        else:
            require(
                a.coordination_contract is not None, "coordination contract required"
            )
            b = validate_binding(read_json(a.binding), real=True)
            scope = {
                "project": "mlo-p4",
                "environment": b["environment"],
                "generation": b["generation"],
                "account_ids": b["account_ids"],
                "state_keys": {
                    r: f"dev/{r}/terraform.tfstate"
                    for r in [*ACCOUNTS, "connectivity", "bootstrap"]
                },
            }
            guard = load_guard(
                a.coordination_contract, scope, "foundation-controller", "create"
            )
            with guard.hold():
                execute(
                    a.stage,
                    a.binding,
                    a.private_dir,
                    enabled=True,
                    operation_guard=guard,
                )
    except (ValueError, KeyError, TypeError, OSError):
        print(
            "FAIL: foundation stage refused/failed; inspect private receipt",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
