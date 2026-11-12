from django.conf import settings
from django.http import JsonResponse

from backend.venues.repositories.curated_venue_detail import configured_sqlite_catalog
from backend.venues.repositories.operational_catalog import CatalogUnavailable, get_operational_venue_repo
from backend.venues.services.ranking_service import filter_and_rank_venues
from backend.venues.services.event_service import check_and_send_nearby_event_promo

from .access import consumer_get


@consumer_get(operational_catalog=True)
def venue_list_api(request, *, consumer):
    """
    GET /api/venues
    - 쿼리 파라미터 기반 매장 탐색 및 Two-Tower 추천 랭킹 API
    """
    gu = (request.GET.get('sigungu') or request.GET.get('gu') or '강남구').strip()
    if any(character in gu for character in ("/", "\\", ":", "\x00")):
        return JsonResponse({'success': False, 'message': '지역 이름을 확인해 주세요.'}, status=400)
    dong = request.GET.get('dong', '').strip()
    category_raw = request.GET.get('category', 'ALL').strip()
    price_tier_raw = request.GET.get('price_tier', '').strip()
    store_type_raw = request.GET.get('store_type', '').strip()
    event_only = request.GET.get('event_only', '0') in ('1', 'true', 'True')
    partner_only = request.GET.get('partner_only', '0') in ('1', 'true', 'True')
    partner_exact = request.GET.get('partner_exact', '0') in ('1', 'true', 'True')
    open_now = request.GET.get('open_now', '0') in ('1', 'true', 'True')
    q = request.GET.get('q', '').strip()
    weather = (request.GET.get('weather') or 'UNKNOWN').strip().upper()
    time_slot = request.GET.get('time_slot', 'AFTERNOON').strip().upper()

    if consumer is None:
        user_id = request.GET.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    else:
        user_id = f'platform:{consumer.pk}'

    # 카테고리 정규화
    cat = 'ALL'
    if category_raw in ('카페', 'CAFE'):
        cat = 'CAFE'
    elif category_raw in ('주점', '술집', 'PUB'):
        cat = 'PUB'

    # 형태 정규화
    prefer_local = None
    if store_type_raw.upper() in ('로컬', '개인', '개인/로컬매장', 'LOCAL'):
        prefer_local = True
    elif store_type_raw.upper() in ('체인', '체인점', '프랜차이즈', 'CHAIN'):
        prefer_local = False

    # 지도 뷰포트(BBox) 파라미터 파싱 (사용자가 보고 있는 맵 영역 기준 검색 지원)
    bbox = None
    try:
        min_lat_s = request.GET.get('min_lat')
        max_lat_s = request.GET.get('max_lat')
        min_lng_s = request.GET.get('min_lng')
        max_lng_s = request.GET.get('max_lng')
        if min_lat_s and max_lat_s and min_lng_s and max_lng_s:
            bbox = (float(min_lat_s), float(max_lat_s), float(min_lng_s), float(max_lng_s))
    except (ValueError, TypeError):
        bbox = None

    # 랭킹 서비스 호출 (조건에 부합하는 전체 후보 매장 추출 후 동/검색어/파트너 필터까지 적용하여 정확한 전수 카운트 산출)
    try:
        rank_options = {
            'gu': gu, 'user_id': user_id, 'category': cat, 'budget_tier': price_tier_raw or None,
            'weather': weather, 'time_slot': time_slot, 'only_with_events': event_only,
            'only_open_now': open_now, 'prefer_local': prefer_local, 'limit': 5000,
            'return_total': True, 'bbox': bbox,
        }
        if not settings.MEMBER_LOCAL_PREVIEW or configured_sqlite_catalog():
            rank_options['venue_repo'] = get_operational_venue_repo()
        results, _ = filter_and_rank_venues(**rank_options)
        if 'venue_repo' in rank_options:
            rank_options['venue_repo'].verify_current()
    except CatalogUnavailable:
        return JsonResponse({'success': False, 'code': 'VENUE_CATALOG_UNAVAILABLE',
                             'message': '매장 카탈로그 연결을 확인해 주세요.'}, status=503)

    # 추가 메모리 필터: 동 단위 & 검색어(q) & 날씨·음악 큐레이션(구 partner_only) 필터
    filtered_results = []
    for item in results:
        if dong and dong not in item.get('dong_name', ''):
            continue
        if partner_exact and not item.get('is_partner'):
            continue
        if partner_only and not (item.get('is_partner') or item.get('recommendation_style')):
            continue
        if q:
            q_lower = q.lower()
            name_match = q_lower in item.get('venue_name', '').lower()
            dong_match = q_lower in item.get('dong_name', '').lower()
            style_match = q_lower in item.get('recommendation_style', '').lower()
            menu_match = any(q_lower in m.get('menu_name', '').lower() for m in item.get('matched_menus_preview', []))
            if not (name_match or dong_match or style_match or menu_match):
                continue

        # active_event 단일 대표 이벤트 추출
        active_events = item.get('active_events', [])
        item['active_event'] = active_events[0] if active_events else None

        filtered_results.append(item)

    total_matched_count = len(filtered_results)
    try:
        display_limit = int(request.GET.get('limit', '100'))
    except ValueError:
        display_limit = 100

    if consumer is None:
        check_and_send_nearby_event_promo(user_id=user_id, gu_name=gu)

    return JsonResponse({
        'success': True,
        'gu': gu,
        'total_count': total_matched_count,
        'venues': filtered_results[:display_limit],
    })
