"""One explicitly consented synthetic claim in the restricted Phase 4 portal."""
from datetime import datetime, timezone as utc_timezone
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from backend.accounts import crypto, policy, services
from backend.accounts.models import PolicyReceipt
from backend.recommendation.errors import require

from . import authorization as auth, worker
from .models import ServiceMembership, WorkspaceClaim

DEMO_USERNAME = "mlo_demo_owner"
REVIEWER_USERNAME = "projectadmin"
POLICY_VERSION = "privacy-handoff-v2"
# Nov 10 through Nov 12 in KST, the exact window approved with this policy bundle.
DEMO_START = datetime(2026, 11, 9, 15, tzinfo=utc_timezone.utc)
DEMO_END = datetime(2026, 11, 12, 15, tzinfo=utc_timezone.utc)
FIXTURE_ID = "demo-active-001"
REVIEW_OPERATIONS = frozenset({"reviewWorkspaceClaim", "decideWorkspaceClaim"})
PRIVATE_REVIEW_OPERATIONS = REVIEW_OPERATIONS | {"listWorkspaceClaimsForReview"}
CLAIM_PAYLOAD = {
    "fixture_id": FIXTURE_ID,
    "display_name": "합성 카페",
    "address": "합성 주소",
    "applicant_relation": "REPRESENTATIVE",
    "_phase4_private_demo": POLICY_VERSION,
}


def demo_window_open(now=None):
    now = timezone.now() if now is None else now
    return DEMO_START <= now < DEMO_END


def review_only_mode():
    return (settings.PHASE4_PRIVATE_DEMO_ENABLED
            and not settings.PLATFORM_ENABLED and not settings.MEMBER_LOCAL_PREVIEW)


def review_ready():
    membership_compatible = not settings.MEMBER_POLICIES_READY or (
        settings.MEMBER_PUBLIC_SIGNUP_ENABLED and policy.signup_policy_ready()
    )
    return (review_only_mode()
            and membership_compatible
            and not settings.PHASE4_OWNER_PORTAL_ENABLED and bool(settings.MEMBER_LOOKUP_KEY)
            and settings.PHASE4_PRIVATE_DEMO_POLICY_VERSION == POLICY_VERSION and demo_window_open()
            and policy.valid_documents(settings.PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS))


def available():
    return review_ready() and not settings.MEMBER_POLICIES_READY


def song_portal_available():
    return (auth.song_portal_only() and not settings.PLATFORM_ENABLED
            and settings.MEMBER_POLICY_VERSION == POLICY_VERSION and demo_window_open()
            and policy.valid_documents(settings.MEMBER_POLICY_DOCUMENTS))


def require_demo_runtime():
    require(available(),
            "PRIVATE_DEMO_NOT_READY", "제한된 시연 점주 등록 설정을 확인해 주세요.", 503)


def require_review_runtime():
    require(review_ready(),
            "PRIVATE_DEMO_NOT_READY", "제한된 시연 점주 심사 설정을 확인해 주세요.", 503)


def reviewer_allowed(user):
    return user.username == REVIEWER_USERNAME and user.has_perm("workspaces.review_workspace")


def review_available_to(user):
    if review_only_mode():
        return review_ready() and reviewer_allowed(user)
    return song_portal_available() and user.has_perm("workspaces.review_workspace")


def require_reviewable(claim):
    if review_only_mode():
        require_review_runtime()
        documents = settings.PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS
    else:
        require(song_portal_available(), "PRIVATE_DEMO_NOT_READY", "제한된 시연 점주 등록 설정을 확인해 주세요.", 503)
        documents = settings.MEMBER_POLICY_DOCUMENTS
    require(claim.fixture_id == FIXTURE_ID and claim.applicant.username == DEMO_USERNAME
            and claim.applicant.is_active and not claim.applicant.is_staff
            and crypto.unseal(claim.payload_ciphertext) == CLAIM_PAYLOAD,
            "NOT_FOUND", "신청을 찾을 수 없습니다.", 404)
    for kind in ("terms", "privacy_notice", "age14"):
        text = policy.receipt_text(documents, kind)
        require(isinstance(text, str) and text and PolicyReceipt.objects.filter(
            user=claim.applicant, version=POLICY_VERSION, kind=kind, accepted=True,
            document_sha256=hashlib.sha256(text.encode()).hexdigest(),
        ).exists(), "DEMO_CONSENT_REQUIRED", "시연 회원 확인 기록이 필요합니다.", 409)


@transaction.atomic
def prepare_claim(password, password_confirmation, *, terms_accepted, privacy_acknowledged, age_confirmed):
    from . import service

    require_demo_runtime()
    signup = {
        "username": DEMO_USERNAME,
        "nickname": "시연점주",
        "password": password,
        "password_confirmation": password_confirmation,
        "policy_version": POLICY_VERSION,
        "terms_accepted": terms_accepted,
        "privacy_acknowledged": privacy_acknowledged,
        "age_confirmed": age_confirmed,
        "email_consent": False,
    }
    documents = settings.PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS
    policy.validate_consent(signup, version=POLICY_VERSION, documents=documents)
    existing = get_user_model().objects.select_for_update().filter(username__iexact=DEMO_USERNAME).first()
    if existing:
        require(existing.username == DEMO_USERNAME and existing.is_active and not existing.is_staff
                and hasattr(existing, "memberprofile") and password == password_confirmation
                and existing.check_password(password),
                "DEMO_ACCOUNT_EXISTS", "기존 계정을 변경할 수 없습니다.", 409)
        auth.business(existing)
        claims = list(WorkspaceClaim.objects.filter(applicant=existing, fixture_id=FIXTURE_ID)[:2])
        require(len(claims) == 1, "DEMO_ACCOUNT_EXISTS", "기존 계정을 변경할 수 없습니다.", 409)
        [claim] = claims
        require_reviewable(claim)
        return service.claim_data(claim)

    profile = services.create_account_after_consent(signup, documents=documents)
    ServiceMembership.objects.create(user=profile.user, kind="BUSINESS")
    context = {"user": profile.user}
    request = SimpleNamespace(request_id=str(uuid4()), headers={})
    created, _ = service.mutate("createWorkspaceClaim", request, CLAIM_PAYLOAD, {}, context)
    claim = WorkspaceClaim.objects.get(pk=created["id"])
    context["claim"] = claim
    request.headers["If-Match"] = f'"{claim.version}"'
    check, _ = service.mutate("requestBusinessCheck", request, {}, {}, context)
    require(worker.run_one(job_id=check["id"]), "BUSINESS_CHECK_REQUIRED", "합성 사업자 확인이 필요합니다.", 409)
    submitted, _ = service.mutate("submitWorkspaceClaim", request, {}, {}, context)
    return submitted
