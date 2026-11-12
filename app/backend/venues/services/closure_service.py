import uuid
from typing import Optional, Tuple
from django.db import transaction
from django.utils import timezone
from backend.venues.models.status_overlay import VenueClosureDecision, VenueStatusOverlay
from backend.venues.models.interaction import VenueInteraction
from backend.venues.models.outbox import VenueOutbox
from backend.venues.streaming.observability import emit_observation

VALID_CLOSURE_STATUSES = frozenset(('OPEN', 'CLOSURE_REPORTED', 'CLOSED_HIDDEN'))


class ClosureValidationError(ValueError):
    """잘못된 폐업 상태 또는 입력 형식 오류"""


class ClosureConflictError(RuntimeError):
    """동시성 버전 충돌 오류"""


def report_venue_closure(
    venue_id: str,
    user_id: str,
    reason: str = '',
    weather_condition: str = '',
    weather_temp: Optional[float] = None,
    *,
    request_id: str = '',
) -> Tuple[VenueStatusOverlay, bool]:
    """
    유저가 매장 폐업 제보 시:
    1. VenueInteraction에 REPORT_CLOSURE 기록
    2. VenueStatusOverlay를 즉시 'CLOSURE_REPORTED'로 전환 및 제보 카운트/버전 갱신
    3. VenueOutbox 및 관측 로그 원자적 적재
    """
    now = timezone.now()
    event_id = str(uuid.uuid4())

    with transaction.atomic():
        VenueInteraction.objects.create(
            user_id=user_id,
            venue_id=venue_id,
            interaction_type='REPORT_CLOSURE',
            weather_condition=weather_condition,
            weather_temp=weather_temp,
            content=reason,
        )

        overlay, created = VenueStatusOverlay.objects.select_for_update().get_or_create(
            venue_id=venue_id,
            defaults={
                'status': 'CLOSURE_REPORTED',
                'closure_report_count': 1,
                'notes': f"[{now.strftime('%Y-%m-%d %H:%M')}] 유저 제보: {reason}",
                'last_reported_at': now,
                'version': 1,
            }
        )

        if not created:
            overlay.closure_report_count += 1
            overlay.last_reported_at = now
            overlay.version += 1
            if overlay.status != 'CLOSED_HIDDEN':
                overlay.status = 'CLOSURE_REPORTED'
            if reason:
                overlay.notes = f"{overlay.notes}\n[{now.strftime('%m-%d %H:%M')}] {reason}".strip()
            overlay.save()

        VenueOutbox.objects.create(
            event_id=event_id,
            event_type='CLOSURE_REPORTED',
            venue_id=venue_id,
            actor_id=str(user_id)[:64],
            request_id=request_id[:64],
            payload={
                'status': overlay.status,
                'report_count': overlay.closure_report_count,
                'version': overlay.version,
            },
        )

    emit_observation(
        dataset='mlo.service',
        action='venue-closure-report',
        outcome='success',
        pipeline_name='venue-closure-workflow',
        stage='report',
        state='QUEUED',
        business_event_id=event_id,
        request_id=request_id,
    )

    return overlay, True


def update_closure_status(
    venue_id: str,
    new_status: str,
    admin_notes: str = '',
    *,
    reviewer=None,
    reason_code: str = 'ADMIN_REVIEW',
    expected_version: Optional[int] = None,
    request_id: str = '',
) -> VenueStatusOverlay:
    """
    관리자 검토 후 상태 변경:
    - 'CLOSED_HIDDEN': 폐업 확정 (검색 및 노출에서 영구 제외)
    - 'OPEN': 정상 영업 복구 (허위 제보 판정)
    - 'CLOSURE_REPORTED': 재확인 대기
    """
    normalized_status = (new_status or '').strip().upper()
    if normalized_status not in VALID_CLOSURE_STATUSES:
        raise ClosureValidationError(f'유효하지 않은 매장 상태입니다: {new_status}')

    now = timezone.now()
    event_id = str(uuid.uuid4())
    cleaned_reason = (reason_code or 'ADMIN_REVIEW').strip()[:48] or 'ADMIN_REVIEW'

    with transaction.atomic():
        overlay, _ = VenueStatusOverlay.objects.select_for_update().get_or_create(venue_id=venue_id)
        if expected_version is not None and overlay.version != expected_version:
            raise ClosureConflictError('다른 관리자가 이미 이 매장의 검수 상태를 변경했습니다. 새로고침 후 다시 확인해 주세요.')

        overlay.status = normalized_status
        overlay.version += 1
        overlay.last_reviewed_by = reviewer
        overlay.last_reviewed_at = now
        overlay.decision_reason_code = cleaned_reason
        if admin_notes:
            now_str = now.strftime('%Y-%m-%d %H:%M')
            overlay.notes = f"{overlay.notes}\n[관리자 처리 {now_str}] {admin_notes}".strip()
        overlay.save()

        VenueClosureDecision.objects.create(
            overlay=overlay,
            overlay_version=overlay.version,
            decision=normalized_status,
            reason_code=cleaned_reason,
            notes=admin_notes,
            reviewer=reviewer,
            request_id=request_id[:64],
        )

        VenueOutbox.objects.create(
            event_id=event_id,
            event_type=f'CLOSURE_RESOLVED_{normalized_status}',
            venue_id=venue_id,
            actor_id=f'admin:{reviewer.pk}' if reviewer else 'admin:system',
            request_id=request_id[:64],
            payload={
                'decision': normalized_status,
                'reason_code': cleaned_reason,
                'version': overlay.version,
            },
        )

    emit_observation(
        dataset='mlo.security',
        action='closure-confirmed' if normalized_status == 'CLOSED_HIDDEN' else 'closure-reopened',
        outcome='success',
        pipeline_name='venue-closure-workflow',
        stage='admin-resolve',
        state='COMMITTED',
        business_event_id=event_id,
        request_id=request_id,
    )

    return overlay
