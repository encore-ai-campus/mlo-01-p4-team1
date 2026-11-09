import csv
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 프로젝트 루트 및 app/ 경로 기준 (app/analysis, 루트 analysis, 워크트리 analysis, 패키지 내 sample_data 모두 자동 지원)
APP_ROOT_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT_DIR = APP_ROOT_DIR.parent
PACKAGE_SAMPLE_DIR = Path(__file__).resolve().parents[1] / 'sample_data'

_ANALYSIS_CANDIDATES = [
    APP_ROOT_DIR / 'analysis',
    REPO_ROOT_DIR / 'analysis',
    Path(__file__).resolve().parents[4] / 'analysis',
]
# 25개 구 전수 CSV(50개 파일)가 모두 들어있는 by_gu 폴더를 최우선으로 선택
BY_GU_DIR = next(
    (
        d / 'seoul_venues_20261106' / 'by_gu'
        for d in _ANALYSIS_CANDIDATES
        if (d / 'seoul_venues_20261106' / 'by_gu').exists()
        and len(list((d / 'seoul_venues_20261106' / 'by_gu').glob('*_venues.csv'))) >= 25
    ),
    next(
        (d / 'seoul_venues_20261106' / 'by_gu' for d in _ANALYSIS_CANDIDATES if (d / 'seoul_venues_20261106' / 'by_gu').exists()),
        APP_ROOT_DIR / 'analysis' / 'seoul_venues_20261106' / 'by_gu',
    ),
)
ANALYSIS_DIR = BY_GU_DIR.parent.parent if BY_GU_DIR.exists() else (APP_ROOT_DIR / 'analysis')

# 폴백 경로 (패키지 내 sample_data/, 루트 analysis/ 및 seoul_venues_20261106/ 하위 모두 지원)
FALLBACK_VENUES_CANDIDATES = [
    PACKAGE_SAMPLE_DIR / 'seoul_venues_demo_sample.csv',
    ANALYSIS_DIR / 'seoul_venues_20261106' / 'seoul_venues_demo_sample.csv',
    ANALYSIS_DIR / 'seoul_venues_demo_sample.csv',
    REPO_ROOT_DIR / 'analysis' / 'seoul_venues_20261106' / 'seoul_venues_demo_sample.csv',
]
FALLBACK_MENUS_CANDIDATES = [
    PACKAGE_SAMPLE_DIR / 'seoul_venue_menus_demo_sample.csv',
    ANALYSIS_DIR / 'seoul_venues_20261106' / 'seoul_venue_menus_demo_sample.csv',
    ANALYSIS_DIR / 'seoul_venue_menus_demo_sample.csv',
    REPO_ROOT_DIR / 'analysis' / 'seoul_venues_20261106' / 'seoul_venue_menus_demo_sample.csv',
]


def _compute_pa_price_tier(avg_price: int, raw_tier: Optional[str] = None) -> int:
    """
    PA 기획안 5단계 가격 구간 산출:
    - 1 (UNDER_10K): 1만원 미만 (< 10,000원)
    - 2 (10K_20K)  : 1만원 이상 ~ 2만원 미만 (10,000 ~ 19,999원)
    - 3 (20K_30K)  : 2만원 이상 ~ 3만원 미만 (20,000 ~ 29,999원)
    - 4 (30K_50K)  : 3만원 이상 ~ 5만원 미만 (30,000 ~ 49,999원)
    - 5 (OVER_50K) : 5만원 이상 (>= 50,000원)
    """
    if raw_tier is not None and str(raw_tier).strip().isdigit():
        t = int(str(raw_tier).strip())
        if 1 <= t <= 5:
            return t
    if avg_price < 10000:
        return 1
    elif avg_price < 20000:
        return 2
    elif avg_price < 30000:
        return 3
    elif avg_price < 50000:
        return 4
    else:
        return 5


class CsvLazyRepository:
    """
    서울시 38,205개 매장 및 611,280개 메뉴 구(Gu) 단위 지연 로딩(Lazy Loading) 저장소.
    - 한글 31컬럼 정본 CSV(utf-8-sig)와 영문 데모 샘플 CSV 스키마를 모두 완벽히 호환.
    - 요청된 구의 CSV 파일만 필요 시점에 파싱하여 인메모리 해시 인덱스에 캐싱.
    """

    def __init__(self):
        self._lock = threading.RLock()
        # gu_name -> List[venue_dict]
        self._gu_venues_cache: Dict[str, List[Dict[str, Any]]] = {}
        # venue_id -> venue_dict
        self._venue_map: Dict[str, Dict[str, Any]] = {}
        # venue_id -> List[menu_dict]
        self._venue_menus_cache: Dict[str, List[Dict[str, Any]]] = {}
        # gu_name -> bool (해당 구의 메뉴 CSV 로드 여부)
        self._gu_menus_loaded: Dict[str, bool] = {}
        # 디스크 원본 재사용 버퍼 (clear_cache 시에도 파일 재파싱 방지)
        self._disk_venues_buffer: Dict[str, List[Dict[str, Any]]] = {}
        self._disk_menus_buffer: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # 폴백 데이터셋 전체 로드 여부
        self._fallback_venues_loaded = False
        self._fallback_menus_loaded = False
        # 기동 시 핵심 상권(강남구) 디스크 버퍼 사전 워밍 (콜드 디스크 I/O 제거)
        self._load_gu_venues_from_disk('강남구')
        self._load_gu_menus_from_disk('강남구')

    def clear_cache(self) -> None:
        """인메모리 캐시 초기화 (단위 테스트 및 리로드용)"""
        with self._lock:
            self._gu_venues_cache.clear()
            self._venue_map.clear()
            self._venue_menus_cache.clear()
            self._gu_menus_loaded.clear()
            self._fallback_venues_loaded = False
            self._fallback_menus_loaded = False

    def _cast_venue_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """한글 31컬럼 실데이터 CSV 및 영문 샘플 CSV를 통합 변환"""
        venue_id = (row.get('venue_id') or '').strip()
        venue_name = (row.get('매장명') or row.get('venue_name') or '').strip()
        gu_name = (row.get('자치구') or row.get('gu_name') or '').strip()
        dong_name = (row.get('행정동') or row.get('dong_name') or '').strip()
        road_address = (row.get('도로명주소') or row.get('road_address') or '').strip()

        raw_cat = (row.get('업종구분') or row.get('category') or '카페').strip().upper()
        if raw_cat in ('술집', '주점', 'PUB', 'BAR'):
            category = 'PUB'
        else:
            category = 'CAFE'

        sub_category = (row.get('세부카테고리') or row.get('sub_category') or '').strip()
        franchise_raw = (row.get('프랜차이즈여부') or '').strip()
        store_type = 'CHAIN' if franchise_raw == '체인점' else 'LOCAL'

        avg_price_raw = row.get('1인_평균예산(원)') or row.get('avg_price') or '10000'
        try:
            avg_price = int(float(str(avg_price_raw).replace(',', '').strip() or 10000))
        except Exception:
            avg_price = 10000

        price_tier = _compute_pa_price_tier(avg_price, row.get('price_tier'))

        # 추천 날씨 정규화 (비 오는 날|흐린 날 -> RAIN 등)
        rec_weather_raw = (row.get('추천날씨') or row.get('primary_weather_affinity') or 'ALL').strip()
        if any(k in rec_weather_raw for k in ('비', 'RAIN', '흐린')):
            primary_weather_affinity = 'RAIN'
        elif any(k in rec_weather_raw for k in ('눈', 'SNOW', '추운')):
            primary_weather_affinity = 'SNOW'
        elif any(k in rec_weather_raw for k in ('맑은', '더운', '선선', 'CLEAR')):
            primary_weather_affinity = 'CLEAR'
        else:
            primary_weather_affinity = 'ALL'

        # 영업시간 & 심야 여부
        open_hours = (row.get('영업시간') or row.get('open_hours') or '매일 09:00-22:00').strip()
        open_time = (row.get('오픈시각') or row.get('open_time') or '09:00').strip()
        close_time = (row.get('마감시각') or row.get('close_time') or '22:00').strip()
        is_late_night = (row.get('심야영업_여부') or 'N').strip().upper() == 'Y'

        if is_late_night or category == 'PUB':
            recommended_time_slot = (row.get('recommended_time_slot') or 'NIGHT').strip().upper()
        else:
            recommended_time_slot = (row.get('recommended_time_slot') or 'AFTERNOON').strip().upper()

        # 분위기 태그 (| 또는 ; 구분자 모두 지원)
        mood_raw = (row.get('매장분위기') or row.get('mood_tags') or '').strip()
        mood_tags = [t.strip() for t in re.split(r'[|;,]', mood_raw) if t.strip()]

        # 파트너 매장 여부 (Y/N 및 True/False 모두 지원)
        partner_raw = str(row.get('파트너매장_여부') or row.get('is_partner') or 'N').strip().lower()
        is_partner = partner_raw in ('y', 'true', '1', 'yes')

        music_mood = (row.get('music_mood') or ('JAZZ' if category == 'PUB' else 'ACOUSTIC')).strip().upper()

        try:
            rating = float(row.get('rating') or 4.6)
        except Exception:
            rating = 4.6

        try:
            review_count = int(float(row.get('review_count') or 42))
        except Exception:
            review_count = 42

        try:
            latitude = float(row.get('위도_lat') or row.get('latitude') or 37.5172)
            longitude = float(row.get('경도_lng') or row.get('longitude') or 127.0473)
        except Exception:
            latitude, longitude = 37.5172, 127.0473

        seat_info = (row.get('좌석_및_편의정보') or '').strip()
        phone_match = re.search(r'전화:([0-9\-]+)', seat_info)
        phone = phone_match.group(1) if phone_match else '02-555-0199'

        return {
            'venue_id': venue_id,
            'venue_name': venue_name,
            'gu_name': gu_name,
            'dong_name': dong_name,
            'road_address': road_address,
            'category': category,
            'sub_category': sub_category,
            'store_type': store_type,
            '프랜차이즈여부': franchise_raw or ('체인점' if store_type == 'CHAIN' else '개인/로컬매장'),
            'price_tier': price_tier,
            'avg_price': avg_price,
            'primary_weather_affinity': primary_weather_affinity,
            '추천날씨': rec_weather_raw,
            'weather_reason': (row.get('날씨별_추천이유') or '').strip(),
            'budget_guide': (row.get('예산별_이용가이드') or '').strip(),
            'open_hours': open_hours,
            'open_time': open_time,
            'close_time': close_time,
            'is_late_night': is_late_night,
            'phone': phone,
            'seat_info': seat_info,
            'recommended_time_slot': recommended_time_slot,
            'mood_tags': mood_tags,
            'music_mood': music_mood,
            'is_partner': is_partner,
            'rating': rating,
            'review_count': review_count,
            'latitude': latitude,
            'longitude': longitude,
            'thumbnail_url': (row.get('매장대표_사진_URL') or '').strip(),
            'interior_photos': [u.strip() for u in (row.get('매장내부_사진_URL_목록') or '').split('|') if u.strip()],
        }

    def _cast_menu_row(self, row: Dict[str, str]) -> Dict[str, Any]:
        """한글 13컬럼 메뉴 CSV 및 영문 메뉴 CSV 통합 변환"""
        menu_id = (row.get('menu_id') or '').strip()
        venue_id = (row.get('venue_id') or '').strip()
        menu_name = (row.get('메뉴명') or row.get('menu_name') or '').strip()

        price_raw = row.get('가격(원)') or row.get('price') or '0'
        try:
            price = int(float(str(price_raw).replace(',', '').strip() or 0))
        except Exception:
            price = 0

        price_tier = _compute_pa_price_tier(price, row.get('price_tier'))

        sig_raw = str(row.get('대표메뉴여부') or row.get('is_signature') or 'N').strip().lower()
        is_signature = sig_raw in ('y', 'true', '1', 'yes')

        weather_tip = (row.get('날씨_및_꿀조합팁') or row.get('weather_match') or 'ALL').strip()
        category = (row.get('메뉴판_카테고리') or row.get('category') or '대표메뉴').strip()

        return {
            'menu_id': menu_id,
            'venue_id': venue_id,
            'menu_name': menu_name,
            'price': price,
            'price_tier': price_tier,
            'is_signature': is_signature,
            'weather_match': weather_tip,
            'description': (row.get('메뉴설명_및_특징') or '').strip(),
            'portion': (row.get('추천인원_및_양') or '1인').strip(),
            'category': category,
            'image_url': (row.get('메뉴사진_URL') or '').strip(),
        }

    def _register_alias_ids(self, idx_1based: int, v: Dict[str, Any]) -> None:
        """VN_SL_000001 및 venue_seoul_0001 양방향 별칭 인덱싱 지원"""
        vid = v['venue_id']
        self._venue_map[vid] = v
        # VN_SL_000001 <-> venue_seoul_0001 호환 매핑
        if vid.startswith('VN_SL_'):
            num_part = vid.replace('VN_SL_', '')
            if num_part.isdigit():
                alias = f"venue_seoul_{int(num_part):04d}"
                self._venue_map.setdefault(alias, v)
        elif idx_1based <= 9999:
            alias = f"venue_seoul_{idx_1based:04d}"
            self._venue_map.setdefault(alias, v)

    def _load_gu_venues_from_disk(self, gu_name: str) -> List[Dict[str, Any]]:
        """특정 구의 매장 CSV 파일 지연 로딩 (utf-8-sig 호환 & 디스크 버퍼 재사용)"""
        if gu_name in self._disk_venues_buffer:
            venues_list = self._disk_venues_buffer[gu_name]
            for idx, v in enumerate(venues_list, start=1):
                self._register_alias_ids(idx, v)
            return list(venues_list)

        gu_venues_file = BY_GU_DIR / f"{gu_name}_venues.csv"
        venues_list: List[Dict[str, Any]] = []

        if gu_venues_file.exists():
            try:
                with open(gu_venues_file, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader, start=1):
                        v = self._cast_venue_row(row)
                        venues_list.append(v)
                        self._register_alias_ids(idx, v)
                self._disk_venues_buffer[gu_name] = list(venues_list)
                logger.info(f"[LazyRepo] Loaded {len(venues_list)} venues for {gu_name} from by_gu CSV")
                return venues_list
            except Exception as e:
                logger.warning(f"[LazyRepo] Failed to read {gu_venues_file}: {e}, falling back.")

        return self._load_fallback_venues_for_gu(gu_name)

    def _load_fallback_venues_for_gu(self, gu_name: str) -> List[Dict[str, Any]]:
        """데모 샘플 CSV에서 특정 구의 매장 로드"""
        if not self._fallback_venues_loaded:
            fallback_path = next((p for p in FALLBACK_VENUES_CANDIDATES if p.exists()), None)
            if fallback_path:
                with open(fallback_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for idx, row in enumerate(reader, start=1):
                        v = self._cast_venue_row(row)
                        self._register_alias_ids(idx, v)
                        self._gu_venues_cache.setdefault(v['gu_name'], []).append(v)
                self._fallback_venues_loaded = True
                logger.info(f"[LazyRepo] Loaded fallback venues from {fallback_path}")
            else:
                logger.error("[LazyRepo] No fallback venues CSV found.")

        return self._gu_venues_cache.get(gu_name, [])

    def _load_gu_menus_from_disk(self, gu_name: str) -> None:
        """특정 구의 메뉴 CSV 파일 지연 로딩 (utf-8-sig 호환)"""
        if self._gu_menus_loaded.get(gu_name):
            return

        if gu_name in self._disk_menus_buffer:
            for k, v in self._disk_menus_buffer[gu_name].items():
                self._venue_menus_cache[k] = v
            self._gu_menus_loaded[gu_name] = True
            return

        gu_menus_file = BY_GU_DIR / f"{gu_name}_menus.csv"
        if gu_menus_file.exists():
            try:
                gu_map: Dict[str, List[Dict[str, Any]]] = {}
                with open(gu_menus_file, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        m = self._cast_menu_row(row)
                        vid = m['venue_id']
                        gu_map.setdefault(vid, []).append(m)
                        if vid.startswith('VN_SL_'):
                            num_part = vid.replace('VN_SL_', '')
                            if num_part.isdigit():
                                alias = f"venue_seoul_{int(num_part):04d}"
                                gu_map.setdefault(alias, []).append(m)
                self._disk_menus_buffer[gu_name] = gu_map
                self._venue_menus_cache.update(gu_map)
                self._gu_menus_loaded[gu_name] = True
                logger.info(f"[LazyRepo] Loaded menus for {gu_name} from by_gu CSV")
                return
            except Exception as e:
                logger.warning(f"[LazyRepo] Failed to read {gu_menus_file}: {e}, falling back to sample.")

        self._load_fallback_menus()
        self._gu_menus_loaded[gu_name] = True

    def _load_fallback_menus(self) -> None:
        """데모 샘플 메뉴 CSV 전체 로드"""
        if not self._fallback_menus_loaded:
            fallback_path = next((p for p in FALLBACK_MENUS_CANDIDATES if p.exists()), None)
            if fallback_path:
                with open(fallback_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        m = self._cast_menu_row(row)
                        vid = m['venue_id']
                        self._venue_menus_cache.setdefault(vid, []).append(m)
                        if vid.startswith('VN_SL_'):
                            num_part = vid.replace('VN_SL_', '')
                            if num_part.isdigit():
                                alias = f"venue_seoul_{int(num_part):04d}"
                                self._venue_menus_cache.setdefault(alias, []).append(m)
                self._fallback_menus_loaded = True
                logger.info(f"[LazyRepo] Loaded fallback menus from {fallback_path}")

    def get_venues_by_gu(self, gu_name: str) -> List[Dict[str, Any]]:
        """구 단위 매장 목록 지연 로딩 조회 (인메모리 캐시 우선)"""
        with self._lock:
            if gu_name in self._gu_venues_cache:
                return self._gu_venues_cache[gu_name]

            venues = self._load_gu_venues_from_disk(gu_name)
            self._gu_venues_cache[gu_name] = venues
            return venues

    def get_venues_by_bbox(
        self,
        min_lat: float,
        max_lat: float,
        min_lng: float,
        max_lng: float,
        fallback_gu: str = '강남구',
    ) -> List[Dict[str, Any]]:
        """
        사용자가 현재 보고 있는 지도 영역(Bounding Box: min_lat~max_lat, min_lng~max_lng) 내의 매장을 반환.
        - 지도 영역과 겹치거나 인접한 구의 CSV를 자동 지연 로딩한 뒤, 실제 위경도가 BBox 안에 들어오는 매장들을 추출합니다.
        """
        gu_centers = {
            '강남구': (37.5172, 127.0286), '서초구': (37.4837, 127.0324), '송파구': (37.5145, 127.1059),
            '강동구': (37.5301, 127.1238), '마포구': (37.5563, 126.9226), '은평구': (37.6176, 126.9227),
            '서대문구': (37.5791, 126.9368), '용산구': (37.5326, 126.9905), '종로구': (37.5730, 126.9794),
            '중구': (37.5641, 126.9979), '성동구': (37.5446, 127.0439), '광진구': (37.5385, 127.0823),
            '영등포구': (37.5264, 126.8962), '관악구': (37.4784, 126.9516), '동작구': (37.5124, 126.9393),
            '강서구': (37.5510, 126.8495), '양천구': (37.5170, 126.8665), '구로구': (37.4954, 126.8874),
            '금천구': (37.4569, 126.8955), '성북구': (37.5894, 127.0167), '동대문구': (37.5744, 127.0398),
            '중랑구': (37.6065, 127.0927), '노원구': (37.6542, 127.0568), '도봉구': (37.6688, 127.0471),
            '강북구': (37.6396, 127.0257),
        }
        center_lat = (min_lat + max_lat) / 2.0
        center_lng = (min_lng + max_lng) / 2.0

        # BBox 여유 반경(약 ±0.06도) 내에 중심점이 들어오는 자치구들을 후보로 선정
        pad = 0.065
        target_gus = [
            g for g, (clat, clng) in gu_centers.items()
            if (min_lat - pad <= clat <= max_lat + pad) and (min_lng - pad <= clng <= max_lng + pad)
        ]
        if not target_gus:
            # 가장 가까운 구 2개 선택
            sorted_gus = sorted(
                gu_centers.keys(),
                key=lambda g: (gu_centers[g][0] - center_lat) ** 2 + (gu_centers[g][1] - center_lng) ** 2,
            )
            target_gus = sorted_gus[:2] if sorted_gus else [fallback_gu]

        with self._lock:
            matched_venues: List[Dict[str, Any]] = []
            seen_ids = set()
            for g in target_gus:
                for v in self.get_venues_by_gu(g):
                    vid = v.get('venue_id')
                    if vid in seen_ids:
                        continue
                    lat = float(v.get('latitude') or 0.0)
                    lng = float(v.get('longitude') or 0.0)
                    if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
                        seen_ids.add(vid)
                        matched_venues.append(v)
            return matched_venues

    def get_venue_by_id(self, venue_id: str) -> Optional[Dict[str, Any]]:
        """매장 단건 고속 조회 (O(1) 해시 룩업, 미로드 시 강남구 기본 로드)"""
        with self._lock:
            if venue_id in self._venue_map:
                return self._venue_map[venue_id]

            # 아직 구가 로드되지 않은 상태라면 대표 구(강남구) 우선 로드
            if '강남구' not in self._gu_venues_cache:
                self.get_venues_by_gu('강남구')
                if venue_id in self._venue_map:
                    return self._venue_map[venue_id]

            # 맵에 없으면 폴백 전체 로드 후 재시도
            if not self._fallback_venues_loaded:
                self._load_fallback_venues_for_gu("")
                if venue_id in self._venue_map:
                    return self._venue_map[venue_id]

            return None

    def get_menus_by_venue(self, venue_id: str) -> List[Dict[str, Any]]:
        """특정 매장의 메뉴판 조회 (인메모리 캐시 O(1) 룩업, 필요시 지연 로딩)"""
        with self._lock:
            if venue_id in self._venue_menus_cache:
                return self._venue_menus_cache[venue_id]

            venue = self.get_venue_by_id(venue_id)
            if venue and venue.get('gu_name'):
                self._load_gu_menus_from_disk(venue['gu_name'])
                if venue_id in self._venue_menus_cache:
                    return self._venue_menus_cache[venue_id]
                real_vid = venue.get('venue_id')
                if real_vid and real_vid in self._venue_menus_cache:
                    return self._venue_menus_cache[real_vid]

            self._load_fallback_menus()
            return self._venue_menus_cache.get(venue_id, [])

    def get_preview_menus_by_venue(
        self,
        venue: Dict[str, Any],
        target_tier: Optional[int] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """추천 카드 하단 가로 슬라이드용 조건 부합 메뉴 프리뷰 조회"""
        vid = venue.get('venue_id', '')
        all_menus = self.get_menus_by_venue(vid)
        matched: List[Dict[str, Any]] = []
        for m in all_menus:
            if target_tier is not None and m.get('price_tier') == target_tier:
                matched.append(m)
            elif target_tier is None and m.get('is_signature'):
                matched.append(m)
        if len(matched) < 4:
            for m in all_menus:
                if m not in matched:
                    matched.append(m)
                if len(matched) >= limit:
                    break
        return matched[:limit]

    def get_all_available_gus(self) -> List[str]:
        """지원 가능한 서울 25개 자치구 목록 반환"""
        with self._lock:
            gus = set(self._gu_venues_cache.keys())
            if BY_GU_DIR.exists():
                for p in BY_GU_DIR.glob('*_venues.csv'):
                    gu = p.name.replace('_venues.csv', '')
                    gus.add(gu)
            if not gus:
                return ['강남구', '마포구', '은평구', '용산구', '성동구', '종로구', '송파구', '영등포구', '서초구', '중구']
            return sorted(list(gus))


# 싱글톤 인스턴스
_repo_instance: Optional[CsvLazyRepository] = None
_instance_lock = threading.Lock()


def get_venue_repo() -> CsvLazyRepository:
    """CsvLazyRepository 글로벌 싱글톤 획득"""
    global _repo_instance
    if _repo_instance is None:
        with _instance_lock:
            if _repo_instance is None:
                _repo_instance = CsvLazyRepository()
    return _repo_instance
