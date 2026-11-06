"""Current DB checks, shared across API writes and asynchronous apply boundaries."""
import hashlib
import hmac
from django.conf import settings
from django.contrib.auth import get_user, get_user_model
from django.db import connection
from django.utils import timezone
from backend.common.identity import raw_request
from backend.recommendation.errors import require
from .models import ServiceMembership, Workspace, WorkspaceMembership


def enabled():
    require(settings.PLATFORM_ENABLED and settings.MEMBER_LOCAL_PREVIEW,
            'PLATFORM_NOT_READY', '로컬 플랫폼 MVP가 활성화되지 않았습니다.', 503)


def dbnow():
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT clock_timestamp()')
            return cursor.fetchone()[0]
    return timezone.now()


def account(request, *, administrator=False):
    raw=raw_request(request)
    cached = get_user(raw)
    require(cached.is_authenticated, 'AUTH_REQUIRED', '로그인이 필요합니다.', 401)
    user=get_user_model().objects.filter(pk=cached.pk,is_active=True).first()
    require(user is not None, 'AUTH_REQUIRED', '로그인이 필요합니다.', 401)
    # Repeat after transaction locks: a password reset must invalidate an already-loaded user.
    supplied=raw.session.get('_auth_user_hash','')
    require(any(hmac.compare_digest(supplied,value) for value in
                [user.get_session_auth_hash(),*user.get_session_auth_fallback_hash()]),
            'AUTH_REQUIRED', '세션이 만료되었습니다. 다시 로그인해 주세요.', 401)
    require(bool(user.is_staff) == administrator, 'FORBIDDEN', '계정 유형의 접근 권한이 없습니다.', 403)
    if not administrator:
        require(hasattr(user, 'memberprofile'), 'AUTH_REQUIRED', '회원 상태를 확인해 주세요.', 401)
    return user


def business(user):
    require(ServiceMembership.objects.filter(user=user, kind='BUSINESS', status='ACTIVE').exists(),
            'BUSINESS_MEMBERSHIP_REQUIRED', '이 계정에 사업자 이용 유형을 먼저 추가해 주세요.', 409)


def workspace(user, identifier, *, lock=False, roles=None, closing=False):
    business(user)
    query = Workspace.objects.select_for_update() if lock else Workspace.objects
    row = query.filter(pk=identifier).first()
    member = WorkspaceMembership.objects.filter(workspace_id=identifier, user=user, status='ACTIVE').first()
    require(row is not None and member is not None, 'NOT_FOUND', '대상을 찾을 수 없습니다.', 404)
    require(row.status in (('ACTIVE','SUSPENDED') if closing else ('ACTIVE',)),
            'WORKSPACE_UNAVAILABLE', '매장 기능이 활성 상태가 아닙니다.', 409)
    require(not roles or member.role in roles, 'FORBIDDEN', '이 작업의 매장 권한이 없습니다.', 403)
    return row, member


def reviewer(request):
    user = account(request, administrator=True)
    require(user.has_perm('workspaces.review_workspace'), 'FORBIDDEN', '매장 심사 권한이 없습니다.', 403)
    session = raw_request(request).session
    at = session.get('admin_passkey_verified_at', 0)
    require(session.get('admin_passkey_user') == str(user.pk) and
            0 <= dbnow().timestamp()-at <= 300,
            'PASSKEY_REQUIRED', '최근 5분 이내 관리자 패스키 인증이 필요합니다.', 403)
    return user


def session_digest(request):
    session = raw_request(request).session
    if not session.session_key:
        session.create()
    return hashlib.sha256(session.session_key.encode()).hexdigest()


def lock_accounts(user_ids):
    # All platform writers use this order before taking workspace/child locks.
    users = list(get_user_model().objects.select_for_update().filter(pk__in=set(user_ids)).order_by('pk'))
    list(ServiceMembership.objects.select_for_update().filter(user_id__in=[u.pk for u in users]).order_by('user_id','kind'))
    require(all(u.is_active for u in users), 'AUTH_REQUIRED', '계정 상태를 확인해 주세요.', 401)
    return users
