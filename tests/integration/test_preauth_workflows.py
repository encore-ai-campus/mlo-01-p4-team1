"""Manual workflow and foundation approval boundaries; no GitHub or AWS calls."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import yaml
from botocore.exceptions import CredentialRetrievalError
from test_preauth_boundary import PrivateInputs, ROOT, digest
from infra_common import oidc
import infra_control
import run_reviewed_workflow as workflow


class ManualWorkflows(PrivateInputs):
    def test_every_effect_workflow_is_private_main_manual_disabled_and_serial(self):
        concurrency = []
        for name, purpose, environment in (
            ("infra-plan", "plan", "infra-plan"),
            ("infra-apply", "apply", "infra-apply"),
            ("app-deploy", "runtime", "infra-runtime"),
            ("infra-destroy", "destroy", "infra-apply"),
        ):
            with self.subTest(name=name):
                value = yaml.load(
                    (ROOT / ".github/workflows" / (name + ".yml")).read_text(),
                    Loader=yaml.BaseLoader,
                )
                self.assertEqual(set(value["on"]), {"workflow_dispatch"})
                self.assertEqual(
                    set(value["on"]["workflow_dispatch"]["inputs"]), {"review_sha256"}
                )
                self.assertNotIn("id-token", value["permissions"])
                job = value["jobs"]["execute"]
                self.assertIn("== 'true'", job["if"])
                self.assertIn("github.ref == 'refs/heads/main'", job["if"])
                self.assertIn("github.event.repository.private == true", job["if"])
                self.assertEqual(job["environment"], environment)
                self.assertEqual(
                    job["runs-on"],
                    ["self-hosted", "Linux", "X64", "mlo-runtime-locked"],
                )
                self.assertEqual(job["permissions"]["id-token"], "write")
                checkout = job["steps"][0]
                self.assertRegex(checkout["uses"], r"^actions/checkout@[a-f0-9]{40}$")
                self.assertEqual(checkout["with"]["persist-credentials"], "false")
                self.assertEqual(job["steps"][1]["env"]["MLO_PURPOSE"], purpose)
                self.assertIn("run_reviewed_workflow.py", job["steps"][1]["run"])
                self.assertEqual(value["concurrency"]["cancel-in-progress"], "false")
                concurrency.append(value["concurrency"]["group"])
        self.assertEqual(len(set(concurrency)), 1)

    def test_authority_public_key_is_pinned_outside_the_review_folder(self):
        key = self.put("authority-public.key", b"local-public-key-placeholder" * 2)
        self.assertEqual(workflow.authority_key(key, digest(key)), key)
        expected = digest(key)
        key.write_bytes(b"attacker-replaced-public-key")
        with self.assertRaises(ValueError):
            workflow.authority_key(key, expected)
        with self.assertRaises(ValueError):
            workflow.authority_key(key, None)

    def test_plan_workflow_cannot_apply_and_destroy_cannot_use_runtime_review(self):
        workflow.target(self.review("foundation", {"stage": "plan"}), "plan")
        workflow.target(self.review("foundation", {"stage": "apply"}), "apply")
        for purpose, review in (
            ("plan", self.review("foundation", {"stage": "apply"})),
            ("apply", self.review("foundation", {"stage": "bootstrap"})),
            ("destroy", self.review("runtime")),
            ("runtime", self.review("destroy")),
        ):
            with self.subTest(purpose=purpose), self.assertRaises(ValueError):
                workflow.target(review, purpose)

    def test_local_workflow_invocation_never_attempts_oidc_or_execution(self):
        with patch.dict(
            os.environ,
            {"MLO_PURPOSE": "apply", "MLO_REVIEW_SHA256": "a" * 64},
            clear=True,
        ), patch.object(workflow, "write_config") as config, patch.object(
            workflow, "execute"
        ) as execute:
            self.assertEqual(workflow.main(), 2)
        config.assert_not_called()
        execute.assert_not_called()

    def test_credential_failure_logs_only_allowlisted_stage(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["infra_control.py", "credential-process"]), patch(
            "sys.stderr", stderr
        ), patch("sys.stdout", stdout), patch.object(
            oidc, "credentials", side_effect=oidc.OIDCPhaseError("token-request")
        ):
            self.assertEqual(infra_control.main(), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(
            workflow.credential_failure_phase(CredentialRetrievalError(
                provider="custom-process", error_msg=stderr.getvalue()
            )),
            "token-request",
        )
        self.assertEqual(
            workflow.credential_failure_phase(CredentialRetrievalError(
                provider="custom-process", error_msg=stderr.getvalue() + "private-value\n"
            )),
            "credential-process",
        )

    def test_foundation_saved_plan_is_bound_to_separate_review(self):
        binding = {
            **self.binding,
            "example_only": False,
            "existing_encore3_reviewed": True,
        }
        # real=True also rejects known fixture values; no account API is involved
        # in this boundary test, so the existing binding validator is injected.
        path = self.put("binding.json", binding)
        contract = self.put("coordination.json", {})
        plans = {}
        for name in ("encore1", "encore2", "encore3", "encore4", "encore5"):
            plans[name] = digest(
                self.put(name + ".tfplan", b"local-plan-" + name.encode())
            )
        payload = {
            "stage": "apply",
            "binding": str(path),
            "binding_sha256": digest(path),
            "private_dir": str(self.folder),
            "coordination_contract": str(contract),
            "coordination_contract_sha256": digest(contract),
            "reviewed_plans": plans,
        }
        gate = self.gate(self.review("foundation", payload))
        self.put("encore3.tfplan", b"replaced-plan")
        with patch("model.validate_binding", return_value=binding), patch(
            "run_foundation.execute"
        ) as actual, patch("infra_common.factory.load_guard") as guard:
            with self.assertRaises(ValueError):
                infra_control.execute(gate, self.folder / "execution-receipt.json")
        actual.assert_not_called()
        guard.assert_not_called()

    def test_coordination_contract_cannot_be_replaced_after_review(self):
        contract = self.put("coordination.json", {})
        payload = {
            "coordination_contract": str(contract),
            "coordination_contract_sha256": digest(contract),
            "coordination_scope": infra_control.scope(self.binding),
        }
        gate = self.gate(self.review("coordination-init", payload))
        self.put("coordination.json", {"accepted": True})
        with patch("infra_common.factory.load_guard") as guard, self.assertRaises(
            ValueError
        ):
            infra_control.execute(gate, self.folder / "execution-receipt.json")
        guard.assert_not_called()
