import json
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt

from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.services.closure_service import update_closure_status
from backend.venues.repositories.csv_lazy_repo import get_venue_repo


@require_GET
def admin_closure_list_api(request):
    """
    GET /api/admin/venues/closures
    - 관리자용 폐업 제보 접수 매장 목록 조회
    """
    overlays = VenueStatusOverlay.objects.filter(
        status__in=['CLOSURE_REPORTED', 'CLOSED_HIDDEN']
    ).order_by('-closure_report_count')

    repo = get_venue_repo()
    results = []
    for o in overlays:
        v = repo.get_venue_by_id(o.venue_id)
        results.append({
            'venue_id': o.venue_id,
            'venue_name': v['venue_name'] if v else '매장',
            'gu_name': v['gu_name'] if v else '',
            'status': o.status,
            'status_label': o.get_status_display(),
            'closure_report_count': o.closure_report_count,
            'last_reported_at': o.last_reported_at.isoformat() if o.last_reported_at else None,
            'notes': o.notes,
        })

    return JsonResponse({
        'success': True,
        'closures': results,
    })


@csrf_exempt
@require_POST
def admin_resolve_closure_api(request, venue_id: str):
    """
    POST /api/admin/venues/closures/<venue_id>/resolve
    - 관리자 폐업 확정(CLOSED_HIDDEN) 또는 정상 영업 복구(OPEN) 처리
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)

    decision = body.get('decision', 'CLOSED_HIDDEN').upper()  # 'CLOSED_HIDDEN' 또는 'OPEN'
    notes = body.get('notes', '')

    overlay = update_closure_status(
        venue_id=venue_id,
        new_status=decision,
        admin_notes=notes,
    )

    return JsonResponse({
        'success': True,
        'message': f"매장 상태가 '{overlay.get_status_display()}'(으)로 갱신되었습니다.",
        'venue_id': venue_id,
        'status': overlay.status,
    })
