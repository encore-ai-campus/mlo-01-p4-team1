from dataclasses import dataclass
from uuid import UUID

from django.contrib.auth import get_user

from backend.common.identity import admin
from backend.recommendation.errors import DomainError
from backend.workspaces.models import ServiceMembership, WorkspaceMembership

from .errors import OperationError, require


@dataclass(frozen=True)
class Principal:
    account_id: str
    capabilities: frozenset[str] = frozenset()


def owner_workspaces(account_id):
    business = ServiceMembership.objects.filter(user_id=account_id, kind='BUSINESS', status='ACTIVE')
    return WorkspaceMembership.objects.filter(
        user_id__in=business.values('user_id'), user__is_active=True,
        role='OWNER', status='ACTIVE', workspace__status='ACTIVE',
    )


def principal(request, capability=None):
    user = get_user(request)
    require(user.is_authenticated and user.is_active, 'AUTH_REQUIRED', '로그인이 필요합니다.', 401)
    if user.is_staff or capability:
        try:
            admin(request, 'manage_music')
        except DomainError as error:
            raise OperationError(error.code, error.message, error.status) from error
        return Principal(str(user.pk), frozenset({'review_songs'}))
    require(owner_workspaces(user.pk).exists(), 'SONG_REQUEST_NOT_ALLOWED',
            '승인된 매장의 점주 계정으로 로그인해 주세요.', 403)
    return Principal(str(user.pk))


def managed_venues(account_id):
    return [{'venue_id': str(row.workspace_id), 'name': row.workspace.display_name, 'address': ''}
            for row in owner_workspaces(account_id).select_related('workspace').order_by('workspace__display_name', 'workspace_id')]


def require_membership(account_id, venue_id):
    try:
        workspace_id = UUID(venue_id)
    except (ValueError, TypeError, AttributeError):
        raise OperationError('INVALID_INPUT', '매장 ID를 확인해 주세요.', 400) from None
    require(owner_workspaces(account_id).filter(workspace_id=workspace_id).exists(),
            'VENUE_PERMISSION_REQUIRED', '승인된 본인 매장을 선택해 주세요.', 403)
    return str(workspace_id)
