from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone

from backend.venues.models.event import VenueEvent
from backend.venues.models.interaction import VenueInteraction
from backend.venues.models.outbox import VenueOutbox
from backend.venues.models.status_overlay import VenueClosureDecision, VenueStatusOverlay
from backend.venues.repositories.csv_lazy_repo import get_venue_repo
from backend.venues.services.closure_service import (
    ClosureConflictError,
    ClosureValidationError,
    update_closure_status,
)
from backend.venues.services.lakehouse_sync import get_lakehouse_runtime_status
from backend.venues.streaming.async_kafka_dispatcher import get_async_dispatcher
from backend.workspaces.models import Workspace, WorkspaceClaim
from .access import admin_api, ensure_request_id, parse_json_body


def _parse_if_match_version(request, body: dict):
    header = request.headers.get('If-Match')
    if header:
        raw = header.strip().strip('"').strip("'")
        if raw == '*':
            return None, None
        try:
            return int(raw), None
        except ValueError:
            return None, JsonResponse({'success': False, 'code': 'INVALID_REQUEST', 'message': 'If-Match 버전 형식이 올바르지 않습니다.'}, status=400)
    if 'version' in body and body['version'] is not None:
        try:
            return int(body['version']), None
        except (TypeError, ValueError):
            return None, JsonResponse({'success': False, 'code': 'INVALID_REQUEST', 'message': 'version 형식이 올바르지 않습니다.'}, status=400)
    return None, None


@admin_api('manage_venues', method='GET')
def admin_closure_list_api(request, *, admin_user):
    """
    GET /api/admin/venues/closures
    - 관리자용 폐업 제보 접수 매장 목록 조회 (상태 필터, 연결 워크스페이스/클레임 정보, 상태별 집계 포함)
    """
    status_filter = (request.GET.get('status') or 'ACTIVE_QUEUE').strip().upper()
    gu_filter = (request.GET.get('gu') or '').strip()

    if status_filter == 'ALL':
        overlays = VenueStatusOverlay.objects.all()
    elif status_filter in ('CLOSURE_REPORTED', 'CLOSED_HIDDEN', 'OPEN'):
        overlays = VenueStatusOverlay.objects.filter(status=status_filter)
    else:
        overlays = VenueStatusOverlay.objects.filter(status__in=['CLOSURE_REPORTED', 'CLOSED_HIDDEN'])

    overlays = list(overlays.order_by('-closure_report_count', '-last_reported_at'))

    # Pending claim count per venue_id (avoids N+1 checks per card)
    pending_claims = WorkspaceClaim.objects.filter(
        state='SUBMITTED',
        fixture_id__startswith='catalog:',
    ).values_list('fixture_id', flat=True)
    pending_by_vid = {}
    for fid in pending_claims:
        vid = fid.split(':', 1)[1]
        pending_by_vid[vid] = pending_by_vid.get(vid, 0) + 1

    active_workspaces = set(
        Workspace.objects.filter(
            location_key__startswith='catalog:',
            status='ACTIVE',
        ).values_list('location_key', flat=True)
    )

    repo = get_venue_repo()
    results = []
    for o in overlays:
        v = repo.get_venue_by_id(o.venue_id)
        v_gu = v['gu_name'] if v else ''
        if gu_filter and gu_filter not in v_gu:
            continue
        results.append({
            'venue_id': o.venue_id,
            'venue_name': v['venue_name'] if v else '매장',
            'gu_name': v_gu,
            'status': o.status,
            'status_label': o.get_status_display(),
            'closure_report_count': o.closure_report_count,
            'version': o.version,
            'last_reported_at': o.last_reported_at.isoformat() if o.last_reported_at else None,
            'last_reviewed_at': o.last_reviewed_at.isoformat() if o.last_reviewed_at else None,
            'decision_reason_code': o.decision_reason_code,
            'notes': o.notes,
            'pending_claim_count': pending_by_vid.get(o.venue_id, 0),
            'has_active_workspace': f'catalog:{o.venue_id}' in active_workspaces,
        })

    counts_map = {
        row['status']: row['total']
        for row in VenueStatusOverlay.objects.values('status').annotate(total=Count('venue_id'))
    }
    return JsonResponse({
        'success': True,
        'closures': results,
        'summary_counts': {
            'closure_reported': counts_map.get('CLOSURE_REPORTED', 0),
            'closed_hidden': counts_map.get('CLOSED_HIDDEN', 0),
            'open': counts_map.get('OPEN', 0),
        },
    })


@admin_api('manage_venues', method='POST', require_recent_passkey=True)
def admin_resolve_closure_api(request, venue_id: str, *, admin_user):
    """
    POST /api/admin/venues/closures/<venue_id>/resolve
    - 관리자 폐업 확정(CLOSED_HIDDEN) 또는 정상 영업 복구(OPEN) 처리 (RBAC + 최근 패스키 + 동시성 버전 검증)
    """
    body, parse_error = parse_json_body(request, required=True)
    if parse_error:
        return parse_error

    expected_version, version_error = _parse_if_match_version(request, body)
    if version_error:
        return version_error

    decision = str(body.get('decision', 'CLOSED_HIDDEN')).strip().upper()
    notes = str(body.get('notes', '') or '').strip()[:500]
    reason_code = str(body.get('reason_code', 'ADMIN_REVIEW') or 'ADMIN_REVIEW').strip()[:48]

    repo = get_venue_repo()
    exists_in_overlay = VenueStatusOverlay.objects.filter(venue_id=venue_id).exists()
    if not exists_in_overlay and repo.get_venue_by_id(venue_id) is None:
        return JsonResponse({'success': False, 'code': 'NOT_FOUND', 'message': '대상 매장을 찾을 수 없습니다.'}, status=404)

    try:
        overlay = update_closure_status(
            venue_id=venue_id,
            new_status=decision,
            admin_notes=notes,
            reviewer=admin_user,
            reason_code=reason_code,
            expected_version=expected_version,
            request_id=ensure_request_id(request),
        )
    except ClosureValidationError as exc:
        return JsonResponse({'success': False, 'code': 'INVALID_DECISION', 'message': str(exc)}, status=400)
    except ClosureConflictError as exc:
        return JsonResponse({'success': False, 'code': 'VERSION_CONFLICT', 'message': str(exc)}, status=409)

    return JsonResponse({
        'success': True,
        'message': f"매장 상태가 '{overlay.get_status_display()}'(으)로 갱신되었습니다.",
        'venue_id': venue_id,
        'status': overlay.status,
        'status_label': overlay.get_status_display(),
        'version': overlay.version,
        'last_reviewed_at': overlay.last_reviewed_at.isoformat() if overlay.last_reviewed_at else None,
        'decision_reason_code': overlay.decision_reason_code,
    })


@admin_api('manage_venues', 'view_operations', method='GET')
def admin_venues_metrics_api(request, *, admin_user):
    """
    GET /api/admin/venues/metrics
    - 매장 폐업 검수·상호작용·이벤트·Outbox·카탈로그 리로드·비동기 스트리밍 큐 운영 지표 API
    """
    status_counts = {
        row['status']: row['total']
        for row in VenueStatusOverlay.objects.values('status').annotate(total=Count('venue_id'))
    }
    interaction_counts = {
        row['interaction_type']: row['total']
        for row in VenueInteraction.objects.values('interaction_type').annotate(total=Count('interaction_id'))
    }
    outbox_counts = {
        row['state']: row['total']
        for row in VenueOutbox.objects.values('state').annotate(total=Count('id'))
    }

    now = timezone.now()
    active_events_count = VenueEvent.objects.filter(is_active=True, end_at__gte=now).count()
    total_events_count = VenueEvent.objects.count()

    return JsonResponse({
        'success': True,
        'closures': {
            'reported': status_counts.get('CLOSURE_REPORTED', 0),
            'hidden': status_counts.get('CLOSED_HIDDEN', 0),
            'open': status_counts.get('OPEN', 0),
            'total_decisions': VenueClosureDecision.objects.count(),
        },
        'interactions': {
            'likes': interaction_counts.get('LIKE', 0),
            'reviews': interaction_counts.get('REVIEW', 0),
            'closure_reports': interaction_counts.get('REPORT_CLOSURE', 0),
            'false_event_reports': interaction_counts.get('REPORT_FALSE_EVENT', 0),
        },
        'events': {
            'active': active_events_count,
            'total': total_events_count,
        },
        'outbox': {
            'pending': outbox_counts.get('PENDING', 0),
            'sent': outbox_counts.get('SENT', 0),
            'failed': outbox_counts.get('FAILED', 0),
        },
        'dispatcher': get_async_dispatcher().get_stats(),
        'catalog': get_venue_repo().get_observability_snapshot(),
        'lakehouse': get_lakehouse_runtime_status(),
        'as_of': now.isoformat().replace('+00:00', 'Z'),
    })
