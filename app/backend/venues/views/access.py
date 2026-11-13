from functools import wraps
import json
import os
from pathlib import Path
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from backend.common.admin_roles import permitted
from backend.common.identity import admin as require_admin_user, raw_request
from backend.recommendation.errors import DomainError, require
from backend.workspaces import authorization
from backend.workspaces.models import ServiceMembership


def standalone_demo():
    return settings.MEMBER_LOCAL_PREVIEW and not settings.PLATFORM_ENABLED


def ensure_request_id(request) -> str:
    rid = getattr(request, 'request_id', None)
    if not rid:
        rid = str(uuid.uuid4())
        request.request_id = rid
    return rid


def parse_json_body(request, *, required: bool = False):
    """Parse UTF-8 JSON object request body cleanly."""
    if not request.body:
        if required:
            return None, JsonResponse({'success': False, 'message': '요청 본문이 필요합니다.'}, status=400)
        return {}, None
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        return None, JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({'success': False, 'message': 'JSON 객체 형식이어야 합니다.'}, status=400)
    return data, None


def _error_response(code: str, message: str, status: int) -> JsonResponse:
    response = JsonResponse({'success': False, 'code': code, 'message': message}, status=status)
    response['Cache-Control'] = 'no-store'
    return response


def _require_member(
    request,
    kind: str,
    error_code: str,
    error_message: str,
    *,
    check_platform_enabled: bool = True,
):
    if check_platform_enabled:
        if kind == 'CONSUMER' and not settings.MEMBER_LOCAL_PREVIEW:
            require(
                settings.PLATFORM_ENABLED,
                'PLATFORM_NOT_READY',
                '로컬 플랫폼 MVP가 활성화되지 않았습니다.',
                503,
            )
        else:
            authorization.enabled()
    user = authorization.account(request)
    require(
        ServiceMembership.objects.filter(user=user, kind=kind, status='ACTIVE').exists(),
        error_code,
        error_message,
        403,
    )
    return user


def _check_admin_access(request, *, permissions, require_recent_passkey: bool):
    user = require_admin_user(request)
    if permissions:
        require(
            any(permitted(user, permission) for permission in permissions),
            'FORBIDDEN',
            '이 기능의 권한이 없습니다.',
            403,
        )
    if require_recent_passkey and settings.ADMIN_PASSKEY_REQUIRED:
        session = raw_request(request).session
        verified_at = session.get('admin_passkey_verified_at', 0)
        try:
            elapsed = timezone.now().timestamp() - float(verified_at)
        except (TypeError, ValueError):
            elapsed = -1
        require(
            session.get('admin_passkey_user') == str(user.pk) and 0 <= elapsed <= 300,
            'PASSKEY_REQUIRED',
            '최근 5분 이내 관리자 패스키 인증이 필요합니다.',
            403,
        )
    return user


def demo_only(view):
    @never_cache
    @wraps(view)
    def checked(request, *args, **kwargs):
        if not standalone_demo():
            return JsonResponse({"success": False, "code": "NOT_FOUND"}, status=404)
        return view(request, *args, **kwargs)

    return checked


def consumer_mcp(view):
    """Keep preview access and require an active consumer for operational MCP."""
    @never_cache
    @wraps(view)
    def checked(request, *args, **kwargs):
        if standalone_demo():
            return view(request, *args, **kwargs)
        if not settings.MEMBER_LOCAL_PREVIEW and not settings.VENUE_MCP_SERVICE_ENABLED:
            return JsonResponse({"success": False, "code": "NOT_FOUND"}, status=404)
        try:
            _require_member(
                request,
                'CONSUMER',
                'CONSUMER_MEMBERSHIP_REQUIRED',
                '사용자 이용 유형을 먼저 추가해 주세요.',
                check_platform_enabled=False,
            )
        except DomainError as error:
            return _error_response(error.code, error.message, error.status)
        if not settings.MEMBER_LOCAL_PREVIEW and getattr(request, "scope", {}).get("venue_mcp_client") is None:
            return JsonResponse({"success": False, "code": "VENUE_MCP_UNAVAILABLE",
                                 "message": "매장 추천 연결을 확인해 주세요."}, status=503)
        return view(request, *args, **kwargs)
    return checked


def consumer_get(
    view=None,
    *,
    empty_when_platform_disabled: bool = False,
    operational_catalog: bool = False,
    public_when_enabled: bool = False,
):
    """Require an active consumer outside standalone demo or explicit public catalog mode."""
    def decorator(fn):
        @never_cache
        @require_GET
        @wraps(fn)
        def checked(request, *args, **kwargs):
            ensure_request_id(request)
            if standalone_demo():
                return fn(request, *args, consumer=None, **kwargs)
            if public_when_enabled and getattr(settings, 'VENUE_PUBLIC_CATALOG_ENABLED', False):
                try:
                    user = _require_member(
                        request,
                        'CONSUMER',
                        'CONSUMER_MEMBERSHIP_REQUIRED',
                        '사용자 이용 유형을 먼저 추가해 주세요.',
                        check_platform_enabled=False,
                    )
                except DomainError:
                    user = None
                return fn(request, *args, consumer=user, **kwargs)
            if not settings.PLATFORM_ENABLED and empty_when_platform_disabled:
                return JsonResponse({
                    'success': True,
                    'user_id': '',
                    'unread_count': 0,
                    'notifications': [],
                    'summary': {
                        'bookmarked_venues': [],
                        'reviews': [],
                        'participations': [],
                        'notifications': [],
                        'unread_notifications_count': 0,
                    },
                })
            try:
                path = os.environ.get('VENUE_MCP_PUBLIC_CATALOG', '').strip()
                catalog_access = (
                    operational_catalog
                    and not settings.MEMBER_LOCAL_PREVIEW
                    and bool(path)
                    and Path(path).suffix.lower() in {'.sqlite', '.sqlite3'}
                )
                user = _require_member(
                    request, 'CONSUMER', 'CONSUMER_MEMBERSHIP_REQUIRED',
                    '사용자 이용 유형을 먼저 추가해 주세요.',
                    check_platform_enabled=not catalog_access,
                )
            except DomainError as error:
                return _error_response(error.code, error.message, error.status)
            return fn(request, *args, consumer=user, **kwargs)

        return checked

    return decorator(view) if view is not None else decorator


def consumer_post(view):
    """Consumer write endpoint guard: open in standalone preview, CSRF+session required in platform mode."""
    @never_cache
    @require_POST
    @wraps(view)
    def checked(request, *args, **kwargs):
        ensure_request_id(request)
        if standalone_demo():
            return view(request, *args, consumer=None, **kwargs)
        try:
            user = _require_member(request, 'CONSUMER', 'CONSUMER_MEMBERSHIP_REQUIRED', '사용자 이용 유형을 먼저 추가해 주세요.')
        except DomainError as error:
            return _error_response(error.code, error.message, error.status)
        return view(request, *args, consumer=user, **kwargs)

    return checked


def business_post(view):
    """Business write endpoint guard: requires ACTIVE BUSINESS membership in integrated/production mode."""
    @never_cache
    @require_POST
    @wraps(view)
    def checked(request, *args, **kwargs):
        ensure_request_id(request)
        if standalone_demo():
            return view(request, *args, business_user=None, **kwargs)
        try:
            user = _require_member(request, 'BUSINESS', 'BUSINESS_MEMBERSHIP_REQUIRED', '이 계정에 사업자 이용 유형을 먼저 추가해 주세요.')
        except DomainError as error:
            return _error_response(error.code, error.message, error.status)
        return view(request, *args, business_user=user, **kwargs)

    return checked


def admin_api(*permissions: str, method: str = 'GET', require_recent_passkey: bool = False):
    """Administrator endpoint guard using shared staff/passkey/RBAC checks across all runtime modes."""
    method_decorator = require_GET if method == 'GET' else require_POST

    def decorator(view):
        @never_cache
        @method_decorator
        @wraps(view)
        def checked(request, *args, **kwargs):
            ensure_request_id(request)
            try:
                user = _check_admin_access(
                    request,
                    permissions=permissions,
                    require_recent_passkey=require_recent_passkey,
                )
            except DomainError as error:
                return _error_response(error.code, error.message, error.status)
            return view(request, *args, admin_user=user, **kwargs)

        return checked

    return decorator
