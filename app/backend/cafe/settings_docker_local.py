"""HTTP-only Docker development settings. Never use for staging or AWS."""
import os

from django.core.exceptions import ImproperlyConfigured
from .settings import *  # noqa: F403,F401

if os.environ.get("MLO_LOCAL_DOCKER") != "1":
    raise ImproperlyConfigured(
        "settings_docker_local is restricted to explicitly enabled local Docker."
    )
if LOCAL_DEMO:  # noqa: F405
    raise ImproperlyConfigured(
        "Docker development must use its isolated PostgreSQL, not the SQLite demo DB."
    )

DEBUG = True
# The Compose services are bound to 127.0.0.1 on the host, never an external interface.
# This override is not imported by the AWS/production DJANGO_SETTINGS_MODULE.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Synthetic local membership policy; never inherited by the production settings.
MEMBER_LOCAL_PREVIEW = True
MEMBER_POLICY_VERSION = 'local-preview-v1'
MEMBER_POLICY_DOCUMENTS = {
    'terms': '로컬 검증용 이용약관입니다. 운영 전에 최종 이용약관을 게시합니다.',
    'privacy_notice': '로컬 검증용 안내: 아이디·비밀번호 해시·별명·카페 유형을 계정 운영에 사용하며 탈퇴 시 정리합니다. 실제 보유·파기 기준은 운영 전에 확정합니다.',
    'email': '복구 이메일은 선택이며 주소 확인·아이디 안내·비밀번호 재설정에 사용합니다. 등록 철회 시 정리하며 거부 시 이메일 복구만 제한됩니다. 실제 운영용 안내는 별도 게시합니다.',
}
MEMBER_MAIL_LIMIT_HOUR = int(os.environ.get('MEMBER_MAIL_LIMIT_HOUR', '10'))
MEMBER_RECOVERY_IP_LIMIT_HOUR = int(os.environ.get('MEMBER_RECOVERY_IP_LIMIT_HOUR', '30'))
MEMBER_LOGIN_LIMIT_HOUR = int(os.environ.get('MEMBER_LOGIN_LIMIT_HOUR', '30'))
MEMBER_PUBLIC_LIMIT_HOUR = int(os.environ.get('MEMBER_PUBLIC_LIMIT_HOUR', '100'))
MEMBER_SECURITY_MAIL_SECONDS = int(os.environ.get('MEMBER_SECURITY_MAIL_SECONDS', '3600'))
MEMBER_PUBLIC_ORIGIN = os.environ.get('MEMBER_PUBLIC_ORIGIN', 'http://localhost:5173')
ADMIN_PASSKEY_RP_ID = os.environ.get('ADMIN_PASSKEY_RP_ID', 'localhost')
ADMIN_PASSKEY_ORIGIN = os.environ.get('ADMIN_PASSKEY_ORIGIN', 'http://localhost:5173')
if PLATFORM_ENABLED:
    SESSION_COOKIE_NAME = 'platform_session'
    CSRF_COOKIE_NAME = 'platform_csrf'
