"""Reviewed local inputs and fixed execution ports; no clients at import time.

The account-authentication boundary is explicit. Local validation never discovers
credentials; live callers must supply a separately signed approval and opt in.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .coordination import canonical, require

ROOT = Path(__file__).resolve().parents[3]
OPERATIONS = {"foundation", "runtime", "destroy", "coordination-init"}


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def private_file(value):
    path = Path(value).absolute()
    require(not path.is_symlink(), "private input symlink refused")
    path = path.resolve()
    require(
        ".private" in path.parts and path.is_file() and not path.stat().st_mode & 0o077,
        "owner-only private file required",
    )
    return path


def read_private(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, "duplicate input field")
            result[key] = item
        return result

    return json.loads(private_file(value).read_text(), object_pairs_hook=unique)


def source_hash(root=ROOT):
    paths = subprocess.check_output(
        ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"], cwd=root
    ).split(b"\0")
    values = {}
    for name in sorted(set(paths)):
        if not name:
            continue
        relative = name.decode()
        path = root / relative
        if path.suffix == ".md" or not path.is_file():
            continue
        require(
            not path.is_symlink() and path.resolve().is_relative_to(root.resolve()),
            "execution source link refused",
        )
        values[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    require(bool(values), "execution sources missing")
    return sha(values)


def validate_review(review):
    require(
        set(review)
        == {
            "schema_version",
            "accepted",
            "example_only",
            "operation",
            "environment",
            "generation",
            "account_ids",
            "source_sha256",
            "payload",
        },
        "execution review fields mismatch",
    )
    require(
        review["schema_version"] == "infra-execution-review-v1"
        and review["operation"] in OPERATIONS
        and review["environment"] == "dev",
        "execution target mismatch",
    )
    require(
        type(review["accepted"]) is bool and type(review["example_only"]) is bool,
        "explicit acceptance flags required",
    )
    require(
        isinstance(review["generation"], str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", review["generation"]),
        "execution generation invalid",
    )
    accounts = review["account_ids"]
    require(
        set(accounts) == {"encore1", "encore2", "encore3", "encore4", "encore5"}
        and len(set(accounts.values())) == 5
        and all(
            isinstance(v, str) and re.fullmatch(r"[0-9]{12}", v)
            for v in accounts.values()
        ),
        "five exact accounts required",
    )
    require(
        re.fullmatch(r"[a-f0-9]{64}", review["source_sha256"]),
        "execution source digest required",
    )
    require(isinstance(review["payload"], dict), "execution payload required")
    return review


class ExecutionGate:
    def __init__(
        self, review_path, approval_path, key_path, *, enabled=False, clock=time.time
    ):
        self.review_path, self.approval_path, self.key_path = map(
            private_file, (review_path, approval_path, key_path)
        )
        self.review = validate_review(read_private(self.review_path))
        self.snapshots = {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.review_path, self.approval_path, self.key_path)
        }
        self.enabled, self.clock = enabled, clock
        self.check()

    def check(self):
        require(self.enabled is True, "explicit live execution opt-in required")
        r = self.review
        require(
            r["accepted"] is True and r["example_only"] is False,
            "reviewed operational inputs required",
        )
        require(source_hash() == r["source_sha256"], "execution source changed")
        for path, digest in self.snapshots.items():
            require(
                private_file(path) == path
                and hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                "execution input changed",
            )
        approval = read_private(self.approval_path)
        require(
            set(approval)
            == {
                "schema_version",
                "review_sha256",
                "operation",
                "reviewer",
                "issued_at",
                "expires_at",
                "signature",
            },
            "approval fields mismatch",
        )
        key = self.key_path.read_bytes()
        require(len(key) == 32, "Ed25519 approval public key required")
        body = {k: v for k, v in approval.items() if k != "signature"}
        require(
            isinstance(approval["signature"], str)
            and re.fullmatch(r"[a-f0-9]{128}", approval["signature"]),
            "approval signature malformed",
        )
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        try:
            Ed25519PublicKey.from_public_bytes(key).verify(
                bytes.fromhex(approval["signature"]), canonical(body)
            )
        except InvalidSignature as exc:
            raise ValueError("approval signature invalid") from exc
        now = self.clock()
        require(
            approval["schema_version"] == "infra-execution-approval-v1"
            and approval["review_sha256"] == sha(r)
            and approval["operation"] == r["operation"]
            and isinstance(approval["reviewer"], str)
            and approval["reviewer"] not in {"", "synthetic-test-authority"},
            "approval target mismatch",
        )
        issued, expires = approval["issued_at"], approval["expires_at"]
        require(
            type(issued) in (int, float)
            and type(expires) in (int, float)
            and issued <= now < expires
            and 0 < expires - issued <= 3600,
            "approval expired or invalid",
        )
        return r


def reserve_receipt(path, review):
    path = Path(path).absolute()
    require(
        ".private" in path.parts and not path.is_symlink() and path.parent.is_dir(),
        "private receipt parent required",
    )
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {
                "schema_version": "infra-execution-receipt-v1",
                "review_sha256": sha(review),
                "status": "pending",
                "ready_for_aws": False,
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    return path


def receipt(path, value):
    temporary = path.with_suffix(".next")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def deploy_runtime(
    gate, clients, receipt_path, *, clock=time.monotonic, pause=time.sleep
):
    """Fixed SSM Document and exact instances; no retry of an uncertain SendCommand.

    The host installer owns the common deploy lease at the effect boundary.
    Holding another deploy lease here would deadlock the remote installer.
    """
    review = gate.check()
    require(review["operation"] == "runtime", "runtime review required")
    payload = review["payload"]
    require(
        set(payload)
        == {
            "account",
            "operation",
            "targets",
            "document",
            "artifact",
            "timeout_seconds",
            "inventory",
            "inventory_sha256",
        },
        "runtime payload fields mismatch",
    )
    account, operation = payload["account"], payload["operation"]
    require(
        account in review["account_ids"]
        and operation in {"install", "deploy", "health", "rollback"},
        "runtime operation mismatch",
    )
    targets = payload["targets"]
    require(
        isinstance(targets, list)
        and targets
        and len(set(targets)) == len(targets)
        and all(re.fullmatch(r"i-[a-f0-9]{17}", v) for v in targets),
        "exact instance IDs required",
    )
    document, artifact = payload["document"], payload["artifact"]
    require(
        set(document) == {"name", "version", "sha256"}
        and document["name"] == "p4-runtime-artifact"
        and re.fullmatch(r"[1-9][0-9]*", document["version"])
        and re.fullmatch(r"[a-f0-9]{64}", document["sha256"]),
        "immutable SSM Document required",
    )
    require(
        set(artifact) == {"bucket", "key", "version", "sha256", "manifest_sha256"}
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", artifact["bucket"])
        and re.fullmatch(r"artifacts/[A-Za-z0-9._/-]+\.tar\.gz", artifact["key"])
        and ".." not in artifact["key"]
        and artifact["version"] not in {"", "null", "latest"}
        and all(
            re.fullmatch(r"[a-f0-9]{64}", artifact[k])
            for k in ("sha256", "manifest_sha256")
        ),
        "immutable artifact required",
    )
    timeout = payload["timeout_seconds"]
    require(type(timeout) is int and 1 <= timeout <= 1800, "runtime timeout invalid")
    inventory_path = private_file(payload["inventory"])
    require(
        hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        == payload["inventory_sha256"],
        "runtime inventory drifted",
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    from check_preparation import validate_inventory

    inventory = validate_inventory(read_private(inventory_path))
    require(
        inventory["example_only"] is False
        and inventory["generation"] == review["generation"],
        "operational inventory required",
    )
    names = {
        n["instance_id"]: n["name"]
        for n in inventory["nodes"]
        if n["account"] == account
    }
    require(
        set(targets) <= set(names), "runtime instance not in accepted account inventory"
    )
    # Factory is invoked only after every local acceptance/shape gate passed.
    sts, ssm, s3 = clients(account)
    identity = sts.get_caller_identity()
    require(
        identity["Account"] == review["account_ids"][account]
        and re.fullmatch(
            r"arn:aws:sts::"
            + identity["Account"]
            + r":assumed-role/p4-controller-"
            + account
            + r"/[A-Za-z0-9+=,.@_-]+",
            identity["Arn"],
        ),
        "runtime account/controller role mismatch",
    )
    described = ssm.describe_document(
        Name=document["name"], DocumentVersion=document["version"]
    )["Document"]
    require(
        described["HashType"] == "Sha256"
        and described["Hash"] == document["sha256"]
        and described["DocumentVersion"] == document["version"],
        "SSM Document drifted",
    )
    content = ssm.get_document(
        Name=document["name"], DocumentVersion=document["version"]
    )["Content"]
    require(
        all(
            artifact[k] in content
            for k in ("key", "version", "sha256", "manifest_sha256")
        ),
        "SSM Document refers to another artifact",
    )
    body = s3.get_object(
        Bucket=artifact["bucket"], Key=artifact["key"], VersionId=artifact["version"]
    )
    try:
        digest = hashlib.sha256()
        chunks = []
        total = 0
        while chunk := body["Body"].read(1024 * 1024):
            total += len(chunk)
            require(total <= 256 * 1024 * 1024, "artifact too large")
            digest.update(chunk)
            chunks.append(chunk)
    finally:
        body["Body"].close()
    require(
        digest.hexdigest() == artifact["sha256"]
        and body.get("VersionId") == artifact["version"],
        "artifact version/hash mismatch",
    )
    sys.path.insert(0, str(ROOT / "aws-infra/contracts"))
    from artifact_verifier import inspect_artifact

    parent = Path(receipt_path).absolute().parent
    require(
        ".private" in parent.parts and parent.is_dir(),
        "private deployment scratch required",
    )
    with tempfile.TemporaryDirectory(prefix="artifact-review-", dir=parent) as folder:
        archive = Path(folder) / "artifact.tar.gz"
        archive.write_bytes(b"".join(chunks))
        archive.chmod(0o600)
        for target in targets:
            manifest = inspect_artifact(
                archive,
                artifact["sha256"],
                review["environment"],
                review["generation"],
                names[target],
                Path(folder) / names[target],
                artifact["manifest_sha256"],
            )
            require(
                manifest.get("fixture", False) is False,
                "lab artifact cannot be deployed",
            )
            node = names[target]
            role = next(n["role"] for n in inventory["nodes"] if n["name"] == node)
            require(
                operation in manifest["operations"]
                and manifest["node_roles"][node] == role
                and manifest["required_secrets"][node]
                and manifest["image_digests"],
                "operational artifact contract incomplete",
            )
    result = {
        "schema_version": "infra-execution-receipt-v1",
        "review_sha256": sha(review),
        "status": "pending",
        "commands": [],
        "ready_for_aws": False,
    }
    path = reserve_receipt(receipt_path, review)
    try:
        for target in targets:
            gate.check()
            require(
                hashlib.sha256(private_file(inventory_path).read_bytes()).hexdigest()
                == payload["inventory_sha256"],
                "runtime inventory replaced",
            )
            result["commands"].append(
                {"instance_id": target, "status": "sending", "command_id": None}
            )
            receipt(path, result)
            command = ssm.send_command(
                InstanceIds=[target],
                DocumentName=document["name"],
                DocumentVersion=document["version"],
                DocumentHash=document["sha256"],
                DocumentHashType="Sha256",
                Parameters={"Operation": [operation]},
                TimeoutSeconds=max(30, timeout),
                Comment="mlo-p4:" + sha(review)[:20],
            )["Command"]["CommandId"]
            current = result["commands"][-1]
            current.update(command_id=command, status="pending")
            receipt(path, result)
            deadline = clock() + timeout
            while clock() < deadline:
                gate.check()
                try:
                    observed = ssm.get_command_invocation(
                        CommandId=command, InstanceId=target
                    )
                except ssm.exceptions.InvocationDoesNotExist:
                    pause(1)
                    continue
                status = observed["Status"]
                if status == "Success":
                    require(observed["ResponseCode"] == 0, "SSM response code mismatch")
                    current["status"] = "success"
                    receipt(path, result)
                    break
                require(
                    status in {"Pending", "InProgress", "Delayed"},
                    "remote runtime failed; inspect host receipt",
                )
                pause(1)
            else:
                raise TimeoutError("remote command outcome uncertain")
        result["status"] = "complete"
        receipt(path, result)
        return result
    except Exception:
        result["status"] = "ambiguous"
        receipt(path, result)
        raise


def validate_destroy_plan(plan, *, delete, retained):
    """Accept only exact reviewed deletes/no-ops; never replacements or backend removal."""
    require(
        isinstance(delete, list)
        and isinstance(retained, list)
        and not set(delete) & set(retained),
        "retention sets overlap",
    )
    actual = set()
    for change in plan.get("resource_changes", []):
        if change.get("mode") != "managed":
            continue
        address, actions = change["address"], change["change"]["actions"]
        require(
            actions in (["delete"], ["no-op"]),
            "non-delete action or replacement refused",
        )
        if actions == ["delete"]:
            require(
                address not in retained and address not in actual,
                "retained/duplicate deletion refused",
            )
            actual.add(address)
    require(actual == set(delete), "saved plan deletion set mismatch")
    return actual


def apply_saved_plan(gate, *, runner=subprocess.run, guard, receipt_path):
    """Execute an already prepared plan; this path never replans or removes state.

    Authentication, service drain, real state and protection release are reviewed
    inputs after the account boundary. The stored plan is checked again immediately
    before apply. A failed or lost effect requires operator reconciliation.
    """
    from .coordination import require_operation

    review = gate.check()
    require(review["operation"] == "destroy", "destroy review required")
    p = review["payload"]
    require(
        set(p)
        == {
            "root",
            "plan",
            "plan_sha256",
            "plan_json",
            "plan_json_sha256",
            "delete",
            "retained",
            "runtime_evidence",
            "backup_evidence",
            "current_state_sha256",
            "terraform",
            "inventory",
            "inventory_sha256",
            "trusted_issuers",
            "trusted_issuers_sha256",
            "snapshots",
            "snapshots_sha256",
            "mode",
            "coordination_contract",
            "coordination_contract_sha256",
            "coordination_scope",
        },
        "destroy payload fields mismatch",
    )
    # Physical data purge/backend retirement require a separate data-erasure
    # acceptance. This adapter intentionally handles reviewed retain-data only.
    require(
        p["mode"] == "retain-data",
        "operational data purge is outside this approval path",
    )
    root = Path(p["root"]).resolve()
    require(
        root.is_relative_to(ROOT / "aws-infra/foundation/terraform/environments/dev")
        and root.name
        in {"encore1", "encore2", "encore3", "encore4", "encore5", "connectivity"},
        "backend/bootstrap or foreign root refused",
    )
    plan, plan_json, evidence_path = map(
        private_file, (p["plan"], p["plan_json"], p["runtime_evidence"])
    )
    require(
        hashlib.sha256(plan.read_bytes()).hexdigest() == p["plan_sha256"]
        and hashlib.sha256(plan_json.read_bytes()).hexdigest() == p["plan_json_sha256"],
        "saved plan inputs drifted",
    )
    validate_destroy_plan(
        read_private(plan_json), delete=p["delete"], retained=p["retained"]
    )
    pinned = {}
    for name in ("inventory", "trusted_issuers", "snapshots"):
        pinned[name] = private_file(p[name])
        require(
            hashlib.sha256(pinned[name].read_bytes()).hexdigest()
            == p[name + "_sha256"],
            "operational input digest mismatch",
        )
    sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "aws-infra/lifecycle/src")]
    from check_preparation import validate_inventory
    from mlo_lifecycle.foundation import review_snapshots, review_root_plan

    inventory = validate_inventory(read_private(pinned["inventory"]))
    require(
        inventory["example_only"] is False
        and inventory["generation"] == review["generation"],
        "live shutdown inventory required",
    )
    snapshots = read_private(pinned["snapshots"])
    for node in inventory["nodes"]:
        address = 'module.account.aws_instance.host["' + node["name"] + '"]'
        require(
            snapshots[node["account"]]["resources"].get(address, {}).get("id")
            == node["instance_id"],
            "snapshot/inventory instance identity mismatch",
        )
    require(
        all(s["source"] == "state" for s in snapshots.values()),
        "mock snapshots cannot authorize AWS deletion",
    )
    partition = review_snapshots(snapshots, p["mode"])
    require(
        partition["generation"] == review["generation"], "snapshot generation mismatch"
    )
    for field, selected in (("delete", p["delete"]), ("retain", p["retained"])):
        expected = {
            a.split(":", 1)[1]
            for a in partition[field]
            if a.startswith(root.name + ":")
        }
        require(set(selected) == expected, "operational retention partition mismatch")
    candidate = read_private(plan_json)
    review_root_plan(partition, snapshots, root.name, candidate, p["plan_sha256"])
    binding = candidate.get("variables", {}).get("binding", {}).get("value", {})
    require(
        binding.get("account_ids") == review["account_ids"]
        and binding.get("generation") == review["generation"]
        and binding.get("environment") == review["environment"]
        and binding.get("example_only") is False,
        "plan bindings differ from approval",
    )
    executable = (ROOT / "aws-infra/foundation/.cache/toolchain/terraform").resolve()
    require(
        Path(p["terraform"]).resolve() == executable, "untrusted Terraform executable"
    )
    token = require_operation(guard, "destroy")
    require(
        token["scope"]["account_ids"] == review["account_ids"]
        and token["scope"]["generation"] == review["generation"],
        "destroy lease mismatch",
    )
    issuer_values = read_private(pinned["trusted_issuers"])
    require(
        issuer_values
        and all(
            isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64,}", v) and len(v) % 2 == 0
            for v in issuer_values.values()
        ),
        "trusted issuer keys required",
    )
    issuers = {name: bytes.fromhex(key) for name, key in issuer_values.items()}

    def observations():
        from .evidence import validate_shutdown

        for name, path in pinned.items():
            require(
                hashlib.sha256(private_file(path).read_bytes()).hexdigest()
                == p[name + "_sha256"],
                "operational input replaced",
            )
        current = require_operation(guard, "destroy")
        validate_shutdown(read_private(evidence_path), inventory, current, issuers)
        backup = read_private(p["backup_evidence"])
        require(
            set(backup)
            == {
                "schema_version",
                "issuer",
                "generation",
                "operation_id",
                "epoch",
                "databases",
                "observed_at",
                "expires_at",
                "signature",
            },
            "backup evidence fields mismatch",
        )
        body = {k: v for k, v in backup.items() if k != "signature"}
        require(
            backup["issuer"] in issuers
            and hmac.compare_digest(
                backup["signature"],
                hmac.new(
                    issuers[backup["issuer"]], canonical(body), hashlib.sha256
                ).hexdigest(),
            ),
            "untrusted backup evidence",
        )
        require(
            backup["schema_version"] == "stateful-restore-evidence-v1"
            and backup["generation"] == review["generation"]
            and backup["operation_id"] == current["operation_id"]
            and backup["epoch"] == current["epoch"],
            "backup observation scope mismatch",
        )
        now = time.time()
        require(
            type(backup["observed_at"]) in (int, float)
            and type(backup["expires_at"]) in (int, float)
            and backup["observed_at"] <= now < backup["expires_at"]
            and 0 < backup["expires_at"] - backup["observed_at"] <= 30,
            "backup evidence expired",
        )
        require(
            set(backup["databases"]) == {"postgresql", "mongodb"},
            "both DB restoration proofs required",
        )
        for name, proof in backup["databases"].items():
            expected = next(
                n["instance_id"] for n in inventory["nodes"] if n["name"] == name
            )
            require(
                set(proof)
                == {
                    "instance_id",
                    "stopped",
                    "backup_version",
                    "backup_sha256",
                    "independent_failure_domain",
                    "restore_verified",
                }
                and proof["instance_id"] == expected
                and proof["stopped"] is True
                and proof["restore_verified"] is True
                and proof["independent_failure_domain"] is True
                and isinstance(proof["backup_version"], str)
                and proof["backup_version"] not in {"", "null"}
                and re.fullmatch(r"[a-f0-9]{64}", proof["backup_sha256"]),
                "DB backup/restore proof incomplete",
            )

    observations()
    path = reserve_receipt(receipt_path, review)
    result = {
        "schema_version": "infra-execution-receipt-v1",
        "review_sha256": sha(review),
        "status": "pending",
        "ready_for_aws": False,
    }
    from credential_bridge import create_isolated_config

    isolated, _, _ = create_isolated_config(
        path.parent / "destroy-sdk",
        review["account_ids"],
        "aws",
        original=os.environ.copy(),
    )
    env = {
        k: v
        for k, v in isolated.items()
        if not k.startswith("TF_CLI_ARGS")
        and k
        not in {
            "TF_LOG",
            "TF_LOG_PATH",
            "TF_WORKSPACE",
            "TF_CLI_CONFIG_FILE",
            "TF_DATA_DIR",
        }
    }
    env.update(TF_WORKSPACE="default", CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1")
    try:
        gate.check()
        require_operation(guard, "destroy")
        require(
            hashlib.sha256(plan.read_bytes()).hexdigest() == p["plan_sha256"],
            "saved plan changed before apply",
        )
        current = runner(
            [str(executable), "state", "pull"],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
        )
        require(
            hashlib.sha256(current.stdout).hexdigest() == p["current_state_sha256"],
            "actual state drifted before apply",
        )
        require(
            sha(json.loads(current.stdout)) == snapshots[root.name]["raw_state_sha256"],
            "current state differs from classified snapshot",
        )
        shown = runner(
            [str(executable), "show", "-json", str(plan)],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
        )
        require(
            sha(json.loads(shown.stdout)) == sha(candidate),
            "actual saved plan differs from reviewed JSON",
        )
        gate.check()
        require_operation(guard, "destroy")
        observations()
        require(
            hashlib.sha256(private_file(plan).read_bytes()).hexdigest()
            == p["plan_sha256"],
            "saved plan replaced at apply boundary",
        )
        runner(
            [str(executable), "apply", "-input=false", "-lock-timeout=60s", str(plan)],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
        )
        gate.check()
        require_operation(guard, "destroy")
        observed = runner(
            [str(executable), "show", "-json"],
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
        )

        def addresses(module):
            own = {
                r["address"]: r.get("values", {}).get("id")
                for r in module.get("resources", [])
                if r.get("mode") == "managed"
            }
            for child in module.get("child_modules", []):
                own.update(addresses(child))
            return own

        remaining = addresses(
            json.loads(observed.stdout)["values"].get("root_module", {})
        )
        require(
            not set(p["delete"]) & set(remaining)
            and all(
                remaining.get(a) == snapshots[root.name]["resources"][a]["id"]
                for a in p["retained"]
            ),
            "post-apply retained/deleted state mismatch",
        )
        result["status"] = "complete"
        receipt(path, result)
        return result
    except Exception:
        result["status"] = "ambiguous"
        receipt(path, result)
        raise
