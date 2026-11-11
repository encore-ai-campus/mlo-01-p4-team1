"""Authentication-boundary tests: SDK requests are stubbed, never sent to AWS."""

import base64
import copy
from datetime import datetime, timezone, timedelta
import gzip
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / p)
    for p in ("scripts", "aws-infra/contracts", "aws-infra/foundation/scripts")
]
import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from infra_common import preauth_execution as execution, transport, oidc
from infra_common.coordination import canonical
from dispatcher_test import artifact_parts
from model import fixture_binding, validate_binding
import infra_control


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class PrivateInputs(unittest.TestCase):
    def setUp(self):
        (ROOT / ".artifacts").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".artifacts")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / ".private"
        self.folder.mkdir(mode=0o700)
        self.binding = fixture_binding()
        self.inventory = json.loads(
            (ROOT / "aws-infra/contracts/inventory.example.json").read_text()
        )
        self.inventory.update(example_only=False, generation="foundation-mock-v1")

    def put(self, name, value):
        path = self.folder / name
        path.write_bytes(value if isinstance(value, bytes) else canonical(value))
        path.chmod(0o600)
        return path

    def review(self, operation="runtime", payload=None):
        return dict(
            schema_version="infra-execution-review-v1",
            accepted=True,
            example_only=False,
            operation=operation,
            environment="dev",
            generation="foundation-mock-v1",
            account_ids=self.binding["account_ids"],
            source_sha256=execution.source_hash(),
            payload=payload or {},
        )

    def gate(self, review, **kwargs):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        signer = Ed25519PrivateKey.from_private_bytes(
            hashlib.sha256(b"LOCAL-TEST-ONLY-SIGNING-SEED").digest()
        )
        key = signer.public_key().public_bytes_raw()
        body = dict(
            schema_version="infra-execution-approval-v1",
            review_sha256=execution.sha(review),
            operation=review["operation"],
            reviewer="local-test-reviewer",
            issued_at=100,
            expires_at=200,
        )
        approval = {**body, "signature": signer.sign(canonical(body)).hex()}
        return execution.ExecutionGate(
            self.put("review.json", review),
            self.put("approval.json", approval),
            self.put("authority.key", key),
            enabled=True,
            clock=lambda: 150,
            **kwargs,
        )


class InputGates(PrivateInputs):
    def test_approval_and_source_are_rechecked_at_each_boundary(self):
        gate = self.gate(self.review())
        self.assertEqual(gate.check()["operation"], "runtime")
        gate.clock = lambda: 201
        with self.assertRaises(ValueError):
            gate.check()
        gate.clock = lambda: 150
        with patch.object(execution, "source_hash", return_value="0" * 64):
            with self.assertRaises(ValueError):
                gate.check()
        gate.approval_path.write_bytes(b"replaced")
        with self.assertRaises(ValueError):
            gate.check()

    def test_unaccepted_example_and_disabled_reviews_are_refused(self):
        for field, value in (("accepted", False), ("example_only", True)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.gate({**self.review(), field: value})
        gate = self.gate(self.review())
        gate.enabled = False
        with self.assertRaises(ValueError):
            gate.check()

    def test_forged_signature_is_refused_without_a_signing_private_key(self):
        gate = self.gate(self.review())
        approval = json.loads(gate.approval_path.read_text())
        approval["signature"] = "0" * 128
        self.put("approval.json", approval)
        with self.assertRaises(ValueError):
            execution.ExecutionGate(
                gate.review_path,
                gate.approval_path,
                gate.key_path,
                enabled=True,
                clock=lambda: 150,
            )

    def test_private_permissions_duplicate_json_and_symlink_fail(self):
        path = self.put("input.json", b'{"same":1,"same":2}')
        with self.assertRaises(ValueError):
            execution.read_private(path)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            execution.private_file(path)
        path.chmod(0o600)
        link = self.folder / "link.json"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            execution.private_file(link)

    def test_receipt_is_exclusive_and_previous_failure_is_preserved(self):
        path = self.folder / "receipt.json"
        execution.reserve_receipt(path, self.review())
        execution.receipt(path, {"status": "ambiguous"})
        with self.assertRaises(FileExistsError):
            execution.reserve_receipt(path, self.review())
        self.assertEqual(json.loads(path.read_text())["status"], "ambiguous")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_offline_prepare_emits_unaccepted_sixteen_host_routes(self):
        binding = self.put("binding.json", self.binding)
        inventory = copy.deepcopy(self.inventory)
        inventory.update(example_only=True, generation=self.binding["generation"])
        inv = self.put("inventory.json", inventory)
        output = self.folder / "prepared"
        with patch(
            "boto3.Session",
            side_effect=AssertionError("offline must not discover credentials"),
        ):
            result = infra_control.prepare(binding, inv, output)
        self.assertFalse(result["aws_calls"])
        self.assertEqual(result["host_contracts"], 16)
        self.assertEqual(result["files"], 36)
        for node in inventory["nodes"]:
            path = output / "hosts" / node["name"] / "coordination.json"
            spec = json.loads(path.read_text())
            self.assertFalse(spec["accepted"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                len(transport.routes(spec, infra_control.scope(self.binding))),
                1 if node["role"] == "gateway" else 2,
            )
        self.assertFalse(
            json.loads((output / "bootstrap-coordination.json").read_text())[
                "single_controller_approved"
            ]
        )

    def test_saved_delete_plan_refuses_replacement_or_retained_resource(self):
        plan = {
            "resource_changes": [
                {
                    "address": "aws_instance.host",
                    "mode": "managed",
                    "change": {"actions": ["delete"]},
                }
            ]
        }
        self.assertEqual(
            execution.validate_destroy_plan(
                plan, delete=["aws_instance.host"], retained=[]
            ),
            {"aws_instance.host"},
        )
        for actions in (["create"], ["update"], ["delete", "create"]):
            bad = copy.deepcopy(plan)
            bad["resource_changes"][0]["change"]["actions"] = actions
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                execution.validate_destroy_plan(
                    bad, delete=["aws_instance.host"], retained=[]
                )
        with self.assertRaises(ValueError):
            execution.validate_destroy_plan(
                plan, delete=[], retained=["aws_instance.host"]
            )

    def test_ci_wildcard_exceptions_cannot_grant_write_or_secret_access(self):
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["ec2:DescribeInstances"],
                    "Resource": ["*"],
                    "Condition": {
                        "StringEquals": {"aws:RequestedRegion": "ap-northeast-2"}
                    },
                }
            ],
        }
        b = copy.deepcopy(self.binding)
        b["oidc_subjects"] = {"plan": "repo:example/project:environment:infra-plan"}
        b["ci_policies"] = {"plan": policy}
        validate_binding(b)
        for action in (
            "ec2:RunInstances",
            "iam:CreateRole",
            "ssm:GetParameter",
            "s3:GetObject",
            "ec2:*",
        ):
            bad = copy.deepcopy(b)
            bad["ci_policies"]["plan"]["Statement"][0]["Action"] = [action]
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_binding(bad)
        b["ci_policies"]["plan"]["Statement"][0].pop("Condition")
        with self.assertRaises(ValueError):
            validate_binding(b)


class STSRoutes(PrivateInputs):
    def spec(self):
        return {
            "node": "control-a",
            "account_alias": "encore1",
            "instance_role_name": "p4-runtime-control-a",
            "sts_route": {
                "mode": "gateway-connect",
                "gateway_addresses": [
                    "http://10.204.101.20:3128",
                    "http://10.204.102.21:3128",
                ],
            },
        }

    def test_imds_v2_ignores_ambient_proxy_and_never_falls_back_to_v1(self):
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://untrusted.invalid:3128",
                "HTTPS_PROXY": "http://untrusted.invalid:3128",
                "NO_PROXY": "",
                "no_proxy": "",
            },
        ):
            provider = transport.metadata_provider()
        fetcher = provider._role_fetcher
        self.addCleanup(fetcher._session.close)
        self.assertIsNone(
            fetcher._session._proxy_config.proxy_url_for(
                "http://169.254.169.254/latest/api/token"
            )
        )
        self.assertTrue(fetcher._imds_v1_disabled)
        self.assertEqual(fetcher._base_url, "http://169.254.169.254/")
        self.assertFalse(fetcher._disabled)

    def test_account_role_topology_and_proxy_url_are_exact(self):
        scope = infra_control.scope(self.binding)
        for mutation in (
            "account",
            "role",
            "public",
            "userinfo",
            "path",
            "duplicate",
            "port",
            "direct",
        ):
            spec = self.spec()
            if mutation == "account":
                spec["account_alias"] = "encore2"
            elif mutation == "role":
                spec["instance_role_name"] = "p4-runtime-control-b"
            elif mutation == "direct":
                spec["sts_route"] = {"mode": "regional-direct"}
            else:
                replacement = {
                    "public": "http://8.8.8.8:3128",
                    "userinfo": "http://user:pass@10.1.1.1:3128",
                    "path": "http://10.1.1.1:3128/path",
                    "duplicate": spec["sts_route"]["gateway_addresses"][1],
                    "port": "http://10.1.1.1:443",
                }[mutation]
                spec["sts_route"]["gateway_addresses"][0] = replacement
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                transport.routes(spec, scope)

    def test_network_failure_switches_approved_proxy_and_closes_both_clients(self):
        first, second = Mock(), Mock()
        first.get_caller_identity.side_effect = EndpointConnectionError(
            endpoint_url=transport.STS_ENDPOINT
        )
        second.get_caller_identity.return_value = {
            "Account": self.binding["account_ids"]["encore1"],
            "Arn": "arn:aws:sts::"
            + self.binding["account_ids"]["encore1"]
            + ":assumed-role/p4-runtime-control-a/local",
        }
        sdk = Mock()
        sdk.client.side_effect = [first, second]
        with patch("boto3.Session", return_value=sdk):
            transport.verify_host(
                botocore.session.Session(),
                self.spec(),
                infra_control.scope(self.binding),
            )
        self.assertEqual(sdk.client.call_count, 2)
        for call, address in zip(
            sdk.client.call_args_list, self.spec()["sts_route"]["gateway_addresses"]
        ):
            self.assertEqual(call.kwargs["endpoint_url"], transport.STS_ENDPOINT)
            self.assertEqual(call.kwargs["config"].proxies, {"https": address})
            self.assertEqual(call.kwargs["config"].retries["total_max_attempts"], 1)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_wrong_identity_never_fails_over_to_another_proxy(self):
        client = Mock()
        client.get_caller_identity.return_value = {
            "Account": "000000000000",
            "Arn": "wrong",
        }
        sdk = Mock()
        sdk.client.return_value = client
        with patch("boto3.Session", return_value=sdk), self.assertRaises(ValueError):
            transport.verify_host(
                botocore.session.Session(),
                self.spec(),
                infra_control.scope(self.binding),
            )
        self.assertEqual(sdk.client.call_count, 1)
        client.close.assert_called_once()


class OIDCExchange(PrivateInputs):
    def setUp(self):
        super().setUp()
        self.account = self.binding["account_ids"]["encore1"]
        self.role = f"arn:aws:iam::{self.account}:role/p4-ci-plan"
        self.repository = "example/project"
        self.context = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": self.repository,
            "GITHUB_REPOSITORY_ID": "456",
            "GITHUB_REPOSITORY_OWNER_ID": "123",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://local.actions.githubusercontent.com/token?request=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "local-test-bearer",
        }
        self.claims = {
            "iss": "https://token.actions.githubusercontent.com",
            "aud": "sts.amazonaws.com",
            "sub": "repo:example@123/project@456:environment:infra-plan",
            "repository_id": "456",
            "repository_owner_id": "123",
            "repository": self.repository,
            "ref": "refs/heads/main",
            "iat": time.time() - 10,
            "exp": time.time() + 60,
        }

    def token(self, claims=None):
        return (
            "header."
            + base64.urlsafe_b64encode(canonical(claims or self.claims))
            .decode()
            .rstrip("=")
            + ".test-signature-aws-verifies"
        )

    def test_claim_audience_environment_ref_account_and_expiry(self):
        oidc.validate_claims(
            self.token(),
            account=self.account,
            role=self.role,
            repository=self.repository,
            environment="infra-plan",
            repository_id="456",
            owner_id="123",
        )
        for field, value in (
            ("aud", "other"),
            ("repository_id", "999"),
            ("repository_owner_id", "999"),
            ("sub", "repo:example/project:environment:infra-plan"),
            ("sub", "repo:other/repository:environment:infra-plan"),
            ("ref", "refs/heads/other"),
            ("exp", 0),
            ("iat", time.time() + 300),
        ):
            bad = {**self.claims, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                oidc.validate_claims(
                    self.token(bad),
                    account=self.account,
                    role=self.role,
                    repository=self.repository,
                    environment="infra-plan",
                    repository_id="456",
                    owner_id="123",
                )

    def test_exact_sts_exchange_is_in_memory_and_config_contains_no_token(self):
        sts = boto3.client(
            "sts",
            region_name="ap-northeast-2",
            aws_access_key_id="local-test-access",
            aws_secret_access_key="local-test-secret",
            config=Config(proxies={}),
        )
        self.addCleanup(sts.close)
        stub = Stubber(sts)
        stub.add_response(
            "assume_role_with_web_identity",
            {
                "Credentials": {
                    "AccessKeyId": "A" * 20,
                    "SecretAccessKey": "S" * 40,
                    "SessionToken": "local-test-session",
                    "Expiration": datetime.now(timezone.utc) + timedelta(minutes=15),
                }
            },
            {
                "RoleArn": self.role,
                "RoleSessionName": "mlo-p4-" + str(os.getpid()),
                "WebIdentityToken": self.token(),
                "DurationSeconds": 900,
            },
        )
        requested = []

        def request(url, headers):
            requested.append((url, headers))
            return {"value": self.token()}

        with stub, patch.dict(os.environ, self.context, clear=True):
            value = oidc.credentials(
                self.account,
                self.role,
                self.repository,
                "infra-plan",
                request=request,
                sts=sts,
            )
        stub.assert_no_pending_responses()
        self.assertEqual(value["Version"], 1)
        self.assertIn("audience=sts.amazonaws.com", requested[0][0])
        self.assertEqual(requested[0][1], {"Authorization": "Bearer local-test-bearer"})
        config = oidc.write_config(
            self.folder / "config",
            self.binding["account_ids"],
            "plan",
            self.repository,
            "infra-plan",
        )
        self.assertEqual(config.read_text().count("credential_process ="), 5)
        self.assertNotIn("local-test-bearer", config.read_text())
        oidc.write_config(
            config, self.binding["account_ids"], "plan", self.repository, "infra-plan"
        )

    def test_non_main_or_untrusted_endpoint_is_rejected_before_request(self):
        for field, value in (
            ("GITHUB_REF", "refs/heads/fork"),
            ("ACTIONS_ID_TOKEN_REQUEST_URL", "https://attacker.example/token"),
        ):
            request = Mock()
            with patch.dict(
                os.environ, {**self.context, field: value}, clear=True
            ), self.assertRaises(ValueError):
                oidc.credentials(
                    self.account,
                    self.role,
                    self.repository,
                    "infra-plan",
                    request=request,
                    sts=Mock(),
                )
            request.assert_not_called()


class SSMDeployment(PrivateInputs):
    def setUp(self):
        super().setUp()
        # Only local port fixtures. No payload is executed or published.
        data, raw = artifact_parts()
        manifest = json.loads(raw)
        manifest["required_secrets"] = {
            n: ["/run/mlo/secrets/local-test"] for n in manifest["target_nodes"]
        }
        manifest["fixture"] = False
        buf = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as old, tarfile.open(
            fileobj=buf, mode="w"
        ) as new:
            for member in old.getmembers():
                content = (
                    canonical(manifest)
                    if member.name == "manifest.json"
                    else old.extractfile(member).read()
                )
                member.size = len(content)
                new.addfile(member, io.BytesIO(content))
        self.archive = gzip.compress(buf.getvalue(), mtime=0)
        inv = self.put("inventory.json", self.inventory)
        self.target = self.inventory["nodes"][0]["instance_id"]
        self.artifact = {
            "bucket": "local-test-transfer",
            "key": "artifacts/local.tar.gz",
            "version": "local-version-1",
            "sha256": hashlib.sha256(self.archive).hexdigest(),
            "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
        }
        self.document = {
            "name": "p4-runtime-artifact",
            "version": "1",
            "sha256": "d" * 64,
        }
        self.payload = {
            "account": "encore1",
            "operation": "install",
            "targets": [self.target],
            "document": self.document,
            "artifact": self.artifact,
            "timeout_seconds": 30,
            "inventory": str(inv),
            "inventory_sha256": digest(inv),
        }
        self.clients = []
        self.stubs = []
        for service in ("sts", "ssm", "s3"):
            client = boto3.client(
                service,
                region_name="ap-northeast-2",
                aws_access_key_id="local-test-access",
                aws_secret_access_key="local-test-secret",
                config=Config(proxies={}, retries={"total_max_attempts": 1}),
            )
            self.addCleanup(client.close)
            self.clients.append(client)
            self.stubs.append(Stubber(client))
        self.command = "00000000-0000-0000-0000-000000000001"

    def preflight(self, account=None, artifact=None):
        account = account or self.binding["account_ids"]["encore1"]
        self.stubs[0].add_response(
            "get_caller_identity",
            {
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/p4-controller-encore1/local",
                "UserId": "local-test-user",
            },
            {},
        )
        self.stubs[1].add_response(
            "describe_document",
            {
                "Document": {
                    "Name": self.document["name"],
                    "DocumentVersion": "1",
                    "HashType": "Sha256",
                    "Hash": self.document["sha256"],
                }
            },
            {"Name": self.document["name"], "DocumentVersion": "1"},
        )
        self.stubs[1].add_response(
            "get_document",
            {"Content": json.dumps(self.artifact)},
            {"Name": self.document["name"], "DocumentVersion": "1"},
        )
        body = artifact if artifact is not None else self.archive
        self.stubs[2].add_response(
            "get_object",
            {
                "VersionId": self.artifact["version"],
                "Body": StreamingBody(io.BytesIO(body), len(body)),
            },
            {
                "Bucket": self.artifact["bucket"],
                "Key": self.artifact["key"],
                "VersionId": self.artifact["version"],
            },
        )

    def send(self, gate, *, failure=False):
        params = {
            "InstanceIds": [self.target],
            "DocumentName": self.document["name"],
            "DocumentVersion": "1",
            "DocumentHash": self.document["sha256"],
            "DocumentHashType": "Sha256",
            "Parameters": {"Operation": ["install"]},
            "TimeoutSeconds": 30,
            "Comment": "mlo-p4:" + execution.sha(gate.review)[:20],
        }
        if failure:
            self.stubs[1].add_client_error(
                "send_command", "InternalServerError", expected_params=params
            )
        else:
            self.stubs[1].add_response(
                "send_command", {"Command": {"CommandId": self.command}}, params
            )

    def invoke(self, gate, **kwargs):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for stub in self.stubs:
                stack.enter_context(stub)
            return execution.deploy_runtime(
                gate,
                lambda account: tuple(self.clients),
                self.folder / "receipt.json",
                **kwargs,
            )

    def test_exact_immutable_artifact_and_document_reach_ssm_then_receipt(self):
        gate = self.gate(self.review(payload=self.payload))
        self.preflight()
        self.send(gate)
        self.stubs[1].add_client_error(
            "get_command_invocation",
            "InvocationDoesNotExist",
            expected_params={"CommandId": self.command, "InstanceId": self.target},
        )
        self.stubs[1].add_response(
            "get_command_invocation",
            {"Status": "Success", "ResponseCode": 0},
            {"CommandId": self.command, "InstanceId": self.target},
        )
        result = self.invoke(gate, pause=lambda _: None)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["commands"][0]["command_id"], self.command)
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_uncertain_send_is_recorded_once_without_resending(self):
        gate = self.gate(self.review(payload=self.payload))
        self.preflight()
        self.send(gate, failure=True)
        with self.assertRaises(Exception):
            self.invoke(gate)
        result = json.loads((self.folder / "receipt.json").read_text())
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(
            result["commands"],
            [{"instance_id": self.target, "status": "sending", "command_id": None}],
        )
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_unknown_target_is_rejected_before_any_client_factory(self):
        self.payload["targets"] = ["i-fffffffffffffffff"]
        gate = self.gate(self.review(payload=self.payload))
        factory = Mock()
        with self.assertRaises(ValueError):
            execution.deploy_runtime(gate, factory, self.folder / "receipt.json")
        factory.assert_not_called()

    def test_main_web_release_rejects_unordered_control_targets_before_aws(self):
        nodes = {node["name"]: node["instance_id"] for node in self.inventory["nodes"]}
        self.payload.update(operation="deploy", targets=[nodes["control-a"], nodes["control-b"]])
        gate = self.gate(self.review(payload=self.payload))
        factory = Mock()
        with self.assertRaisesRegex(ValueError, "exact ordered Control targets"):
            execution.deploy_runtime(
                gate, factory, self.folder / "receipt.json",
                web_release=("main-" + "a" * 40, "unused"),
            )
        factory.assert_not_called()

    def test_main_web_release_rejects_another_signed_image_before_send(self):
        nodes = {node["name"]: node["instance_id"] for node in self.inventory["nodes"]}
        self.payload.update(operation="deploy", targets=[nodes["control-b"], nodes["control-a"]])
        gate = self.gate(self.review(payload=self.payload))
        release = "main-" + "a" * 40
        self.preflight()
        with patch("artifact_verifier.inspect_artifact", return_value={
            "fixture": False, "artifact_version": release, "image_digests": {"web": "signed-other-image"},
        }), self.assertRaisesRegex(ValueError, "differs from published main web release"):
            self.invoke(gate, web_release=(release, "published-image"))
        self.assertFalse((self.folder / "receipt.json").exists())
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_archive_hash_mismatch_never_reaches_send(self):
        gate = self.gate(self.review(payload=self.payload))
        self.preflight(artifact=b"corrupt")
        with self.assertRaises(ValueError):
            self.invoke(gate)
        self.assertFalse((self.folder / "receipt.json").exists())
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_remote_failure_or_timeout_preserves_command_for_reconciliation(self):
        gate = self.gate(self.review(payload=self.payload))
        self.preflight()
        self.send(gate)
        self.stubs[1].add_response(
            "get_command_invocation",
            {"Status": "Failed", "ResponseCode": 1},
            {"CommandId": self.command, "InstanceId": self.target},
        )
        with self.assertRaises(ValueError):
            self.invoke(gate)
        self.assertEqual(
            json.loads((self.folder / "receipt.json").read_text())["commands"][0][
                "command_id"
            ],
            self.command,
        )
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_timeout_keeps_command_id_and_does_not_resend(self):
        gate = self.gate(self.review(payload=self.payload))
        self.preflight()
        self.send(gate)
        ticks = iter([0, 31])
        with self.assertRaises(TimeoutError):
            self.invoke(gate, clock=lambda: next(ticks))
        result = json.loads((self.folder / "receipt.json").read_text())
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["commands"][0]["command_id"], self.command)
        for stub in self.stubs:
            stub.assert_no_pending_responses()

    def test_wrong_account_cannot_reach_document_or_artifact_api(self):
        gate = self.gate(self.review(payload=self.payload))
        account = self.binding["account_ids"]["encore2"]
        self.stubs[0].add_response(
            "get_caller_identity",
            {
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/p4-controller-encore1/local",
                "UserId": "local-test-user",
            },
            {},
        )
        with self.assertRaises(ValueError):
            self.invoke(gate)
        self.assertFalse((self.folder / "receipt.json").exists())
        for stub in self.stubs:
            stub.assert_no_pending_responses()
