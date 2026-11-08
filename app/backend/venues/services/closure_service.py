from typing import Any, Dict, Optional, Tuple
from django.utils import timezone
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.models.interaction import VenueInteraction


def report_venue_closure(
    venue_id: str,
    user_id: str,
    reason: str = '',
    weather_condition: str = '',
    weather_temp: Optional[float] = None,
) -> Tuple[VenueStatusOverlay, bool]:
    """
    유저가 매장 폐업 제보 시:
    1. VenueInteraction에 REPORT_CLOSURE 기록
    2. VenueStatusOverlay를 즉시 'CLOSURE_REPORTED'로 전환 및 제보 카운트 +1
    """
    now = timezone.now()

    # 1. 제보 상호작용 기록
    VenueInteraction.objects.create(
        user_id=user_id,
        venue_id=venue_id,
        interaction_type='REPORT_CLOSURE',
        weather_condition=weather_condition,
        weather_temp=weather_temp,
        content=reason,
    )

    # 2. 오버레이 상태 업데이트
    overlay, created = VenueStatusOverlay.objects.get_or_create(
        venue_id=venue_id,
        defaults={
            'status': 'CLOSURE_REPORTED',
            'closure_report_count': 1,
            'notes': f"[{now.strftime('%Y-%m-%d %H:%M')}] 유저 제보: {reason}",
            'last_reported_at': now,
        }
    )

    if not created:
        overlay.closure_report_count += 1
        overlay.last_reported_at = now
        # 이미 폐업 확정(CLOSED_HIDDEN)이 아니라면 제보 접수 상태로 점등
        if overlay.status != 'CLOSED_HIDDEN':
            overlay.status = 'CLOSURE_REPORTED'
        if reason:
            overlay.notes = f"{overlay.notes}\n[{now.strftime('%m-%d %H:%M')}] {reason}".strip()
        overlay.save()

    return overlay, True


def update_closure_status(
    venue_id: str,
    new_status: str,
    admin_notes: str = '',
) -> VenueStatusOverlay:
    """
    관리자 검토 후 상태 변경:
    - 'CLOSED_HIDDEN': 폐업 확정 (검색 및 노출에서 영구 제외)
    - 'OPEN': 정상 영업 복구 (허위 제보 판정)
    - 'CLOSURE_REPORTED': 재확인 대기
    """
    overlay, _ = VenueStatusOverlay.objects.get_or_create(venue_id=venue_id)
    overlay.status = new_status
    if admin_notes:
        now_str = timezone.now().strftime('%Y-%m-%d %H:%M')
        overlay.notes = f"{overlay.notes}\n[관리자 처리 {now_str}] {admin_notes}".strip()
    overlay.save()
    return overlay
