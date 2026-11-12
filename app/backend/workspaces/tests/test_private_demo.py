import hashlib
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase, override_settings

from backend.accounts import crypto
from backend.accounts.models import PolicyReceipt
from backend.recommendation.errors import DomainError
from backend.workspaces import private_demo
from backend.workspaces.authorization import dbnow
from backend.workspaces.models import ClaimDecision, Workspace, WorkspaceClaim

from .test_platform import TEST_CONFIG, PASSWORD

PRIVATE_DOCUMENTS = {
    "terms": "Internal approved terms",
    "privacy_notice": "Internal approved notice",
    "email": "Internal approved optional email",
}


@override_settings(**{
    **TEST_CONFIG,
    "LOCAL_DEMO": False,
    "PLATFORM_ENABLED": False,
    "MEMBER_LOCAL_PREVIEW": False,
    "MEMBER_POLICY_VERSION": "",
    "MEMBER_POLICY_DOCUMENTS": {},
    "MEMBER_POLICIES_READY": False,
    "PHASE4_OWNER_PORTAL_ENABLED": False,
    "PHASE4_PRIVATE_DEMO_ENABLED": True,
    "PHASE4_PRIVATE_DEMO_POLICY_VERSION": private_demo.POLICY_VERSION,
    "PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS": PRIVATE_DOCUMENTS,
    "ADMIN_PASSKEY_REQUIRED": True,
})
class PrivateDemoEnrollmentTests(TestCase):
    def setUp(self):
        window = patch.object(private_demo, "demo_window_open", return_value=True)
        window.start()
        self.addCleanup(window.stop)

    def prepare_claim(self, *, age_confirmed=True):
        return private_demo.prepare_claim(
            PASSWORD, PASSWORD, terms_accepted=True, privacy_acknowledged=True,
            age_confirmed=age_confirmed,
        )

    def post(self, client, path, body, *, revision=None):
        csrf = client.get("/api/auth/me").json()["csrf_token"]
        headers = {
            "HTTP_X_CSRFTOKEN": csrf,
            "HTTP_IDEMPOTENCY_KEY": str(uuid4()),
        }
        if revision is not None:
            headers["HTTP_IF_MATCH"] = f'"{revision}"'
        return client.post(path, body, content_type="application/json", **headers)

    def reviewer(self, username):
        reviewer = get_user_model().objects.create_user(username=username, password=PASSWORD, is_staff=True)
        reviewer.user_permissions.add(Permission.objects.get(
            content_type__app_label="workspaces", codename="review_workspace",
        ))
        client = Client(enforce_csrf_checks=True)
        client.force_login(reviewer)
        return reviewer, client

    def verify_passkey_session(self, reviewer, client, *, elapsed_seconds=0):
        session = client.session
        session["admin_passkey_user"] = str(reviewer.pk)
        session["admin_passkey_verified_at"] = (dbnow() - timedelta(seconds=elapsed_seconds)).timestamp()
        session.save()

    def test_private_policy_and_signup_do_not_leak_through_public_routes(self):
        public = Client(enforce_csrf_checks=True)
        policy = public.get("/api/accounts/policy")
        self.assertEqual(policy.json(), {"version": "", "ready": False, "local_preview": False, "documents": {}})
        self.assertEqual(public.get("/api/platform/v1/policies").status_code, 503)
        self.assertEqual(self.post(public, "/api/accounts/signup", {}).status_code, 404)
        self.assertEqual(self.post(public, "/api/platform/v1/signups", {}).status_code, 503)
        self.assertEqual(self.post(public, "/api/platform/v1/workspace-claims", {}).status_code, 503)

        with self.assertRaises(DomainError) as caught:
            self.prepare_claim(age_confirmed=False)
        self.assertEqual(caught.exception.code, "AGREEMENT_REQUIRED")
        self.assertFalse(get_user_model().objects.filter(username=private_demo.DEMO_USERNAME).exists())

    def test_separate_public_policy_preserves_approved_demo_review(self):
        claim = self.prepare_claim()
        reviewer, admin = self.reviewer(private_demo.REVIEWER_USERNAME)
        self.verify_passkey_session(reviewer, admin)
        path = f"/api/platform/v1/admin/workspace-claims/{claim['id']}"
        approval = self.post(admin, path + "/decisions", {
            "decision": "APPROVE", "reason_code": "DEMO_SCENARIO_CONFIRMED",
        }, revision=claim["revision"])
        self.assertEqual(approval.status_code, 200, approval.content)

        public = Client(enforce_csrf_checks=True)
        docs = {"terms": "Public terms fixture", "privacy_notice": "Public notice fixture",
                "email": "Public email fixture"}
        with override_settings(MEMBER_POLICIES_READY=True, MEMBER_POLICY_VERSION="public-policy-fixture",
                               MEMBER_POLICY_DOCUMENTS=docs):
            self.assertFalse(public.get("/api/accounts/policy").json()["ready"])
            self.assertEqual(self.post(public, "/api/accounts/signup", {}).status_code, 404)
            with override_settings(MEMBER_PUBLIC_SIGNUP_ENABLED=True):
                with override_settings(MEMBER_PUBLIC_LIMIT_HOUR=0):
                    self.assertFalse(public.get("/api/accounts/policy").json()["ready"])
                    self.assertEqual(self.post(public, "/api/accounts/signup", {}).status_code, 503)
                self.assertTrue(public.get("/api/accounts/policy").json()["ready"])
                self.assertEqual(public.get("/api/accounts/policy").json()["documents"], docs)
                self.assertEqual(admin.get(path).status_code, 200)
                self.assertTrue(ClaimDecision.objects.filter(claim_id=claim["id"], decision="APPROVE").exists())
                self.assertFalse(private_demo.available())
                body = {"username": "publicfixture", "nickname": "공개회원", "password": PASSWORD,
                        "password_confirmation": PASSWORD, "terms_accepted": True,
                        "privacy_acknowledged": True, "age_confirmed": True, "email_consent": False}
                stale = self.post(public, "/api/accounts/signup", body | {"policy_version": private_demo.POLICY_VERSION})
                self.assertEqual(stale.status_code, 409, stale.content)
                created = self.post(public, "/api/accounts/signup", body | {"policy_version": "public-policy-fixture"})
                self.assertEqual(created.status_code, 201, created.content)
                self.assertEqual(set(PolicyReceipt.objects.filter(user__username="publicfixture")
                                     .values_list("version", flat=True)), {"public-policy-fixture"})
                login = self.post(public, "/api/auth/login", {"username": "publicfixture", "password": PASSWORD})
                self.assertEqual(login.status_code, 200, login.content)
                self.assertTrue(public.get("/api/auth/me").json()["authenticated"])
                withdrawal = self.post(public, "/api/me/withdraw", {
                    "current_password": PASSWORD, "confirmation": "회원탈퇴",
                })
                self.assertEqual(withdrawal.status_code, 200, withdrawal.content)
                self.assertFalse(public.get("/api/auth/me").json()["authenticated"])
                self.assertFalse(get_user_model().objects.filter(username="publicfixture").exists())
                self.assertFalse(PolicyReceipt.objects.filter(version="public-policy-fixture").exists())
                self.assertEqual(admin.get(path).status_code, 200)

    def test_exact_reviewer_and_private_receipts_without_public_portal(self):
        claim = self.prepare_claim()
        self.assertEqual(claim, self.prepare_claim())
        self.assertEqual(claim["state"], "SUBMITTED")
        self.assertFalse(Workspace.objects.exists())
        owner = get_user_model().objects.get(username=private_demo.DEMO_USERNAME)
        documents = {**PRIVATE_DOCUMENTS, "age14": "만 14세 이상 확인"}
        self.assertEqual(
            dict(PolicyReceipt.objects.filter(user=owner, version=private_demo.POLICY_VERSION)
                 .values_list("kind", "document_sha256")),
            {kind: hashlib.sha256(documents[kind].encode()).hexdigest()
             for kind in ("terms", "privacy_notice", "age14")},
        )

        path = f"/api/platform/v1/admin/workspace-claims/{claim['id']}"
        queue = "/api/platform/v1/admin/workspace-claims"
        self.assertEqual(Client().get(queue).status_code, 401)
        other, other_client = self.reviewer("other-reviewer")
        WorkspaceClaim.objects.create(
            applicant=other, fixture_id=private_demo.FIXTURE_ID, state="SUBMITTED",
            payload_ciphertext=crypto.seal(private_demo.CLAIM_PAYLOAD),
        )
        self.verify_passkey_session(other, other_client)
        self.assertEqual(other_client.get(path).status_code, 403)
        self.assertEqual(other_client.get(queue).status_code, 403)
        menu = {"path": "/platform/review", "label": "매장 권한 심사"}
        self.assertNotIn(menu, other_client.get("/api/auth/me").json()["allowed_menus"])

        reviewer, client = self.reviewer(private_demo.REVIEWER_USERNAME)
        self.assertEqual(client.get(path).status_code, 403)
        self.assertEqual(client.get(queue).status_code, 403)
        self.assertNotIn(menu, client.get("/api/auth/me").json()["allowed_menus"])
        self.verify_passkey_session(reviewer, client, elapsed_seconds=301)
        self.assertEqual(client.get(queue).json()["error"]["code"], "PASSKEY_REQUIRED")

        self.verify_passkey_session(reviewer, client)
        account = client.get("/api/auth/me").json()
        self.assertTrue(account["private_demo_review_allowed"])
        self.assertIn(menu, account["allowed_menus"])
        self.assertEqual(client.get(f"/api/platform/v1/admin/workspace-claims/{uuid4()}").status_code, 404)
        detail = client.get(path).json()["data"]
        self.assertEqual(detail["review_context"]["policy_version"], private_demo.POLICY_VERSION)
        response = client.get(queue)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"], {"items": [detail], "next_cursor": None})
        self.assertIn("no-store", response["Cache-Control"])

        response = self.post(client, path + "/decisions", {
            "decision": "APPROVE", "reason_code": "DEMO_SCENARIO_CONFIRMED",
        }, revision=detail["revision"])
        self.assertEqual(response.status_code, 200, response.content)
        decision = ClaimDecision.objects.get(claim_id=claim["id"])
        self.assertEqual(decision.reviewer, reviewer)
        self.assertEqual(decision.policy_version, private_demo.POLICY_VERSION)
        self.assertEqual(
            [(item["id"], item["state"]) for item in client.get(queue).json()["data"]["items"]],
            [(claim["id"], "APPROVED")],
        )

        permission = Permission.objects.get(content_type__app_label="workspaces", codename="review_workspace")
        reviewer.user_permissions.remove(permission)
        self.assertEqual(client.get(queue).status_code, 403)
        self.assertNotIn(menu, client.get("/api/auth/me").json()["allowed_menus"])
        reviewer.user_permissions.add(permission)

        owner_client = Client()
        owner_client.force_login(owner)
        self.assertEqual(owner_client.get("/api/platform/v1/sessions/current").status_code, 503)
        self.assertEqual(owner_client.get("/api/phase4/session").status_code, 403)
        self.assertEqual(owner_client.get(queue).status_code, 403)
        with patch.object(private_demo, "demo_window_open", return_value=False):
            account = client.get("/api/auth/me").json()
            self.assertFalse(account["private_demo_review_allowed"])
            self.assertNotIn(menu, account["allowed_menus"])
            self.assertEqual(client.get(path).status_code, 503)
            self.assertEqual(client.get(queue).status_code, 503)

    def test_review_queue_checks_scenario_and_consent_before_returning_claim(self):
        claim = self.prepare_claim()
        reviewer, client = self.reviewer(private_demo.REVIEWER_USERNAME)
        self.verify_passkey_session(reviewer, client)
        queue = "/api/platform/v1/admin/workspace-claims"
        row = WorkspaceClaim.objects.get(pk=claim["id"])
        ciphertext = row.payload_ciphertext
        row.payload_ciphertext = crypto.seal({**private_demo.CLAIM_PAYLOAD, "display_name": "Other"})
        row.save(update_fields=["payload_ciphertext"])
        self.assertEqual(client.get(queue).status_code, 404)

        row.payload_ciphertext = ciphertext
        row.save(update_fields=["payload_ciphertext"])
        PolicyReceipt.objects.filter(user=row.applicant, kind="terms").delete()
        response = client.get(queue)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "DEMO_CONSENT_REQUIRED")
