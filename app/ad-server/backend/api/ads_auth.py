from dataclasses import dataclass

from django.conf import settings
from django.utils.module_loading import import_string
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import PermissionDenied

from domains.ads.errors import DomainError


@dataclass(frozen=True)
class AdminIdentity:
    account_id: str
    identity_source: str
    role: str
    active: bool
    ads_allowed: bool


def enforce_admin_csrf(request):
    """Validate a same-origin token/cookie pair without changing global settings.

    The common host uses platform_csrf in platform mode, while legacy hosts use
    csrftoken. Only configured cookie names are tried, and every attempt retains
    Django's Origin/Referer and token checks. Request-local aliases are restored.
    """
    primary = settings.CSRF_COOKIE_NAME
    names = (primary, *getattr(settings, "ADS_CSRF_COOKIE_ALIASES", ()))
    original_cookies = request.COOKIES
    metadata = {
        name: (name in request.META, request.META.get(name))
        for name in ("CSRF_COOKIE", "CSRF_COOKIE_NEEDS_UPDATE")
    }
    try:
        for name in dict.fromkeys(names):
            if name != primary and (settings.CSRF_USE_SESSIONS or name not in original_cookies):
                continue
            request.COOKIES = (
                original_cookies
                if name == primary
                else {**original_cookies, primary: original_cookies[name]}
            )
            try:
                SessionAuthentication().enforce_csrf(request)
                return
            except PermissionDenied:
                continue
        raise PermissionDenied("CSRF verification failed.")
    finally:
        request.COOKIES = original_cookies
        for name, (present, value) in metadata.items():
            if present:
                request.META[name] = value
            else:
                request.META.pop(name, None)


def authorize_admin(request, action: str) -> AdminIdentity:
    """공유 인증 담당자의 서버 어댑터만 허용합니다. 클라이언트 ID/역할은 읽지 않습니다."""
    path = settings.ADS_ADMIN_AUTHORIZER
    if not path:
        raise DomainError("AUTH_UNAVAILABLE", "공유 관리자 인증이 연결되지 않았습니다.", 503)
    try:
        identity = import_string(path)(request._request, action)
    except DomainError:
        raise
    except Exception:
        raise DomainError("AUTH_UNAVAILABLE", "관리자 인증을 확인할 수 없습니다.", 503) from None
    if identity is None:
        raise DomainError("AUTH_REQUIRED", "관리자 로그인이 필요합니다.", 401)
    if not isinstance(identity, AdminIdentity):
        raise DomainError("AUTH_UNAVAILABLE", "관리자 인증 응답이 유효하지 않습니다.", 503)
    if identity.active is not True:
        raise DomainError("AUTH_REQUIRED", "관리자 인증이 만료되었거나 비활성 상태입니다.", 401)
    if identity.role != "ADMIN" or identity.ads_allowed is not True:
        raise DomainError("FORBIDDEN", "광고 관리 권한이 없습니다.", 403)
    if not all(
        isinstance(value, str) and value.strip()
        for value in (identity.account_id, identity.identity_source)
    ):
        raise DomainError("AUTH_UNAVAILABLE", "관리자 인증 참조가 유효하지 않습니다.", 503)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        try:
            enforce_admin_csrf(request._request)
        except PermissionDenied:
            raise DomainError(
                "FORBIDDEN", "요청 출처 또는 CSRF 검증에 실패했습니다.", 403
            ) from None
    return identity
