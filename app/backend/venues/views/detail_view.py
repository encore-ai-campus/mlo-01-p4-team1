from typing import Any, Dict, List
from django.http import JsonResponse

from backend.venues.repositories.csv_lazy_repo import get_venue_repo
from backend.venues.repositories.curated_venue_detail import curated_venue_detail, local_catalog_detail
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.models.interaction import VenueInteraction
from backend.venues.services.open_hours_service import is_venue_open_now
from backend.venues.services.music_banner_service import get_venue_music_banner
from backend.venues.services.event_service import get_visible_events_for_venue

from backend.workspaces.music import public_applied_playlist
from .access import consumer_get


@consumer_get
def venue_detail_api(request, venue_id: str, *, consumer):
    """
    GET /api/venues/<venue_id>
    - 매장 상세 정보, 5개 탭 전체 메뉴판, 음악 배너, 실시간 이벤트 정보 반환
    """
    if curated := local_catalog_detail(venue_id):
        if curated.get('_catalog_blocked'):
            return JsonResponse({'success': False, 'message': '매장을 찾을 수 없습니다.'}, status=404)
        if VenueStatusOverlay.objects.filter(venue_id=venue_id, status='CLOSED_HIDDEN').exists():
            return JsonResponse({'success': False, 'message': '매장을 찾을 수 없습니다.'}, status=404)
        if consumer is not None:
            curated['applied_playlist'] = public_applied_playlist(venue_id)
        return JsonResponse({"success": True, "venue": curated})
    if consumer is None and (curated := curated_venue_detail(venue_id)):
        return JsonResponse({"success": True, "venue": curated})

    repo = get_venue_repo()
    venue = repo.get_venue_by_id(venue_id)

    if not venue:
        return JsonResponse({
            'success': False,
            'message': '매장을 찾을 수 없습니다.',
        }, status=404)

    canonical_id = venue['venue_id']
    overlay = VenueStatusOverlay.objects.filter(venue_id=canonical_id).first()
    if overlay and overlay.status == 'CLOSED_HIDDEN':
        return JsonResponse({'success': False, 'message': '매장을 찾을 수 없습니다.'}, status=404)

    status_code = overlay.status if overlay else 'OPEN'
    status_label = overlay.get_status_display() if overlay else '정상 영업'

    # 2. 지금 영업 중 판정
    is_open = is_venue_open_now(venue)

    # 3. 실제 확인된(수집·조사·브랜드) 메뉴판 조회 및 5개 탭 분류 (배민 스타일, 공통 가짜 템플릿 비노출)
    lookup_id = canonical_id if consumer else venue_id
    if hasattr(repo, 'get_verified_menus_by_venue'):
        all_menus = repo.get_verified_menus_by_venue(lookup_id)
    else:
        all_menus = repo.get_menus_by_venue(lookup_id)
    cat = venue.get('category', 'CAFE').upper()

    tabs_dict: Dict[str, List[Dict[str, Any]]] = {}
    if cat == 'CAFE':
        tab_names = ['시그니처/대표', '커피/에스프레소', '논커피/티', '베이커리/디저트', '브런치/사이드']
    else:
        tab_names = ['시그니처/대표', '수제생맥주/주류', '하이볼/와인', '메인요리/안주', '사이드/스낵']

    for name in tab_names:
        tabs_dict[name] = []

    for idx, m in enumerate(all_menus):
        # 시그니처 탭
        if m.get('is_signature'):
            tabs_dict[tab_names[0]].append(m)

        # 4개 일반 카테고리 탭 분배
        slot = 1 + (idx % 4)
        tabs_dict[tab_names[slot]].append(m)

    menu_tabs = [{'tab_name': k, 'items': v} for k, v in tabs_dict.items()]

    # 4. 파트너 상태·적용 플레이리스트·음악 배너 (Option A)
    from django.conf import settings
    from backend.venues.models.bridge import VenueOwnerBridge
    from backend.venues.services.music_banner_service import expand_venue_id_aliases
    from backend.workspaces.models import Workspace

    candidate_ids = expand_venue_id_aliases([canonical_id, venue_id])
    bridges = list(VenueOwnerBridge.objects.filter(venue_id__in=candidate_ids))
    verified_bridge = next((b for b in bridges if b.is_verified), None)
    music_bridge = next((b for b in bridges if b.track1_partner_music_id), None) or verified_bridge
    active_workspace_exists = Workspace.objects.filter(
        status='ACTIVE',
        location_key__in=[f'catalog:{cid}' for cid in candidate_ids],
    ).exists()

    applied_playlist = None
    if consumer is not None or not settings.PLATFORM_ENABLED:
        for cid in candidate_ids:
            applied_playlist = public_applied_playlist(cid)
            if applied_playlist:
                break

    is_partner = bool(
        venue.get('is_partner', False)
        or verified_bridge is not None
        or active_workspace_exists
        or applied_playlist is not None
    )

    current_weather = request.GET.get('weather') or 'UNKNOWN'
    music_banner = get_venue_music_banner(
        venue,
        current_weather=current_weather,
        bridge=music_bridge,
        applied_playlist=applied_playlist,
    )

    # 5. 실시간 진행 중(또는 5분 이내 종료) 이벤트 목록
    visible_events = get_visible_events_for_venue(venue_id)

    recent_reviews = list(VenueInteraction.objects.filter(
        venue_id__in={venue_id, canonical_id},
        interaction_type='REVIEW',
    ).order_by('-created_at')[:10].values(
        'interaction_id', 'user_id', 'content', 'rating', 'weather_condition', 'weather_temp', 'created_at'
    ))
    for review in recent_reviews:
        if review['user_id'].startswith('platform:'):
            review['user_id'] = '방문 회원'
        if hasattr(review['created_at'], 'isoformat'):
            review['created_at'] = review['created_at'].isoformat()

    return JsonResponse({
        'success': True,
        'venue': {
            **venue,
            'venue_id': venue_id,
            'canonical_venue_id': canonical_id,
            'is_partner': is_partner,
            'applied_playlist': applied_playlist,
            'status_code': status_code,
            'status_label': status_label,
            'is_open_now': is_open,
            'has_closure_warning': (status_code == 'CLOSURE_REPORTED'),
            'music_banner': music_banner,
            'active_events': visible_events,
            'menu_tabs': menu_tabs,
            'all_menus_count': len(all_menus),
            'has_verified_menus': len(all_menus) > 0,
            'recent_reviews': recent_reviews,
        }
    })
