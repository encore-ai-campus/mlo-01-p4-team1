import numpy as np
from typing import Any, Dict, List, Optional, Union

from backend.venues.repositories.csv_lazy_repo import get_venue_repo
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.services.open_hours_service import is_venue_open_now
from backend.venues.services.music_banner_service import get_venue_music_banner
from backend.venues.services.event_service import get_visible_events_for_venue
from backend.venues.dl_engine.feature_encoder_18d import get_feature_encoder
from backend.venues.dl_engine.micro_two_tower import get_micro_two_tower
from backend.venues.dl_engine.realtime_user_memory import get_user_memory


def filter_and_rank_venues(
    gu: str,
    user_id: Optional[str] = None,
    category: str = 'ALL',
    budget_tier: Optional[Union[int, str]] = None,
    weather: str = 'CLEAR',
    time_slot: str = 'AFTERNOON',
    only_with_events: bool = False,
    only_open_now: bool = False,
    prefer_local: Optional[bool] = None,
    mood_tags: Optional[List[str]] = None,
    limit: int = 30,
    return_total: bool = False,
    bbox: Optional[tuple[float, float, float, float]] = None,
) -> Union[List[Dict[str, Any]], tuple[List[Dict[str, Any]], int]]:
    """
    Track 2 매장 탐색, 필터링 및 Two-Tower 결합 랭킹 서비스.
    - PA 5단계 가격 필터 적용
    - 폐업 확정(CLOSED_HIDDEN) 매장 완전 제외
    - 조건 부합 메뉴 가로 프리뷰 스트립용 상위 메뉴 바인딩 (matched_menus_preview)
    - Two-Tower DL 점수와 규칙 기반 점수 앙상블 (50:50)
    """
    repo = get_venue_repo()
    encoder = get_feature_encoder()
    two_tower = get_micro_two_tower()
    user_memory = get_user_memory()

    # 1. 지도 뷰포트(BBox) 또는 구 단위 매장 목록 지연 로딩
    if bbox is not None:
        min_lat, max_lat, min_lng, max_lng = bbox
        raw_venues = repo.get_venues_by_bbox(min_lat, max_lat, min_lng, max_lng, fallback_gu=gu)
    else:
        raw_venues = repo.get_venues_by_gu(gu)
    if not raw_venues:
        return ([], 0) if return_total else []

    # 2. 폐업 상태 오버레이 일괄 조회
    venue_ids = [v['venue_id'] for v in raw_venues]
    overlays = {
        o.venue_id: o
        for o in VenueStatusOverlay.objects.filter(venue_id__in=venue_ids)
    }

    # 3. 유저 16D 취향 벡터 준비
    if user_id:
        user_16d = user_memory.get_or_init_user_vector(user_id)
    else:
        # 비회원 게스트: 현재 컨텍스트 기반 임시 벡터 생성
        u_feat = encoder.encode_user_context(
            weather=weather,
            time_slot=time_slot,
            budget_tier=budget_tier or 2,
            preferred_tastes=mood_tags or [],
            prefer_local=True if prefer_local is None else prefer_local,
        )
        user_16d = two_tower.forward_user(u_feat)

    # 3-1. PA 5단계 가격 타깃 티어 파싱
    target_tier: Optional[int] = None
    if budget_tier and str(budget_tier).upper() != 'ALL':
        mapping = {
            '1': 1, 'UNDER_10K': 1,
            '2': 2, '10K_20K': 2,
            '3': 3, '20K_30K': 3,
            '4': 4, '30K_50K': 4,
            '5': 5, 'OVER_50K': 5,
        }
        target_tier = mapping.get(str(budget_tier).upper())

    # 4. 필터링 및 점수 계산 루프
    candidates = []

    for v in raw_venues:
        v_id = v['venue_id']

        # [필터 1] 폐업 확정 매장 제외
        ov = overlays.get(v_id)
        if ov and ov.status == 'CLOSED_HIDDEN':
            continue

        # [필터 2] 카테고리 필터
        if category and category.upper() != 'ALL':
            if v.get('category', '').upper() != category.upper():
                continue

        # [필터 3] PA 5단계 가격 필터
        if target_tier is not None:
            if v.get('price_tier', 1) != target_tier:
                continue

        # [필터 4] 로컬/체인 형태 필터 (실데이터 프랜차이즈여부 / store_type 기준)
        if prefer_local is not None:
            is_chain = (v.get('store_type') == 'CHAIN') or (v.get('프랜차이즈여부') == '체인점') or any(
                c in v.get('venue_name', '') for c in ['스타벅스', '투썸', '이디야', '메가커피', '컴포즈', '역전할머니']
            )
            if prefer_local and is_chain:
                continue
            if not prefer_local and not is_chain:
                continue

        # [필터 5] 지금 영업 중 필터
        is_open = is_venue_open_now(v)
        if only_open_now and not is_open:
            continue

        # [필터 6] 이벤트 진행 매장만 필터
        events = get_visible_events_for_venue(v_id)
        has_active_event = any(not e.get('is_ended', False) for e in events)
        if only_with_events and not has_active_event:
            continue

        # 5. 메뉴 목록 조회 및 조건 부합 메뉴 추출 (matched_menus_preview — 대표메뉴 초고속 슬라이싱)
        all_menus = repo.get_preview_menus_by_venue(v, target_tier=target_tier, limit=6)
        matched_preview = all_menus[:6]

        # 6. Two-Tower DL 임베딩 코사인 유사도 점수 (0.0 ~ 1.0 정규화)
        v_feat = encoder.encode_venue(v)
        v_emb = two_tower.forward_venue(v_feat)
        cos_sim = two_tower.compute_similarity(user_16d, v_emb)  # [-1.0, 1.0]
        dl_score = max(0.0, (cos_sim + 1.0) / 2.0)               # [0.0, 1.0]

        # 7. 규칙 기반 도메인 점수 계산
        rule_score = 0.5
        # 날씨 친화도 가산점
        w_aff = v.get('primary_weather_affinity', 'ALL')
        if w_aff == weather.upper() or w_aff == 'ALL':
            rule_score += 0.2
        # 평점 가산점 (4.0 ~ 5.0 -> 0.0 ~ 0.2)
        rating = float(v.get('rating', 4.5))
        rule_score += max(0.0, (rating - 4.0) * 0.2)
        # 파트너 매장 및 이벤트 가산점
        if v.get('is_partner'):
            rule_score += 0.1
        if has_active_event:
            rule_score += 0.15

        rule_score = min(1.0, rule_score)

        # 8. 앙상블 최종 점수 (DL 50% + Rule 50%)
        final_score = (dl_score * 0.5) + (rule_score * 0.5)

        # 상태 오버레이 라벨
        status_label = '정상 영업'
        status_code = 'OPEN'
        if ov:
            status_code = ov.status
            status_label = ov.get_status_display()

        # 파트너 음악 배너
        music_banner = get_venue_music_banner(v, current_weather=weather)

        candidates.append({
            'venue_id': v_id,
            'venue_name': v.get('venue_name'),
            'gu_name': v.get('gu_name'),
            'dong_name': v.get('dong_name'),
            'road_address': v.get('road_address'),
            'thumbnail_url': v.get('thumbnail_url') or '',
            'interior_photos': v.get('interior_photos') or [],
            'category': v.get('category'),
            'sub_category': v.get('sub_category'),
            'price_tier': v.get('price_tier'),
            'avg_price': v.get('avg_price'),
            'rating': v.get('rating'),
            'review_count': v.get('review_count'),
            'is_partner': v.get('is_partner', False),
            'mood_tags': v.get('mood_tags', []),
            'music_mood': v.get('music_mood'),
            'latitude': v.get('latitude'),
            'longitude': v.get('longitude'),
            'is_open_now': is_open,
            'status_code': status_code,
            'status_label': status_label,
            'has_closure_warning': (status_code == 'CLOSURE_REPORTED'),
            'music_banner': music_banner,
            'active_events': events,
            'matched_menus_preview': matched_preview,
            'match_score': round(final_score * 100, 1),
            'dl_similarity': round(cos_sim, 4),
        })

    # 9. 최종 점수 내림차순 정렬
    candidates.sort(key=lambda x: x['match_score'], reverse=True)
    if return_total:
        return candidates[:limit], len(candidates)
    return candidates[:limit]


def rank_venues(
    venues: List[Dict[str, Any]],
    user_id: Optional[str] = None,
    weather: str = 'CLEAR',
    time_slot: str = 'AFTERNOON',
) -> List[Dict[str, Any]]:
    """이미 필터링된 매장 리스트 재정렬"""
    return venues
