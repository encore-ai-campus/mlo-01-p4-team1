import numpy as np
from typing import Any, Dict, List, Optional, Union

CHAIN_FLAG_VALUES = frozenset({'체인점', 'Y', '프랜차이즈', 'CHAIN', 'TRUE', '1'})
LOCAL_BRAND_DEFAULTS = frozenset({'', '개인매장', '단독매장'})


def is_chain_venue(venue: Dict[str, Any]) -> bool:
    """Return True when a venue belongs to a franchise chain by normalized type, flag, or brand."""
    franchise_val = str(venue.get('프랜차이즈여부', '')).strip().upper()
    brand_val = str(venue.get('brand_name') or venue.get('브랜드명') or '개인매장').strip()
    return (
        venue.get('store_type') == 'CHAIN'
        or franchise_val in CHAIN_FLAG_VALUES
        or brand_val not in LOCAL_BRAND_DEFAULTS
    )


class FeatureEncoder18D:
    """
    Track 2 18차원 원-핫 / 소프트 멀티-핫 특성 인코더.
    - [0~1]   실시간 날씨 최소 게이트 2칸 (강수/실내 vs 맑음/청량)
    - [2~4]   영업 시간대 3칸 (주간 vs 저녁피크 vs 심야)
    - [5~7]   PA 5단계 예산 구간 소프트 매핑 3칸
    - [8~13]  세부 매장/메뉴 취향 6칸 (스페셜티/카공, 디저트/베이커리, 한옥/루프탑/뷰, 펍/생맥주, 와인/이자카야/하이볼, 포차/전집/국물)
    - [14~15] 로컬/개인 vs 프랜차이즈/체인 2칸
    - [16~17] 파트너 음악 & 공간 무드 2칸 (차분/어쿠스틱 vs 활기찬/업템포)
    """

    DIM = 18

    # 취향 매핑 키워드 사전
    TASTE_KEYWORDS = {
        8: ['스페셜티', '핸드드립', '카공', '작업', '조용한', '원두', '로스터리', '에스프레소'],
        9: ['디저트', '베이커리', '수플레', '스콘', '케이크', '타르트', '크루아상', '팬케이크', '달콤'],
        10: ['한옥', '루프탑', '야경', '뷰', '테라스', '데이트', '감성', '아늑한', '전망'],
        11: ['생맥주', '크래프트', '수제맥주', '탭룸', '호프', '펍', '치킨', '피맥', '바이닐'],
        12: ['이자카야', '와인', '하이볼', '위스키', '바', '칵테일', '비스트로', '하몽', '치즈'],
        13: ['포차', '전집', '파전', '김치전', '막걸리', '국물', '어묵', '짬뽕탕', '소주', '안주', '비오는날감성'],
    }

    def encode_budget_tier(self, budget_tier: Union[int, str]) -> np.ndarray:
        """
        PA 5단계 가격 구간 소프트 원-핫 매핑 (Module 06 5절 반영):
        - UNDER_10K (1) -> [1.0, 0.0, 0.0]
        - 10K_20K   (2) -> [0.0, 1.0, 0.0]
        - 20K_30K   (3) -> [0.0, 0.5, 0.5]  (경계 구간 소프트 분배)
        - 30K_50K   (4) -> [0.0, 0.0, 1.0]
        - OVER_50K  (5) -> [0.0, 0.0, 1.0]
        """
        vec = np.zeros(3, dtype=np.float32)
        tier_str = str(budget_tier).upper()
        if tier_str in ('1', 'UNDER_10K'):
            vec[0] = 1.0
        elif tier_str in ('2', '10K_20K'):
            vec[1] = 1.0
        elif tier_str in ('3', '20K_30K'):
            vec[1] = 0.5
            vec[2] = 0.5
        elif tier_str in ('4', '30K_50K', '5', 'OVER_50K'):
            vec[2] = 1.0
        else:
            # 기본 중간값
            vec[1] = 0.7
            vec[2] = 0.3
        return vec

    def encode_weather_gate(self, weather: str) -> np.ndarray:
        """
        [0~1] 실시간 날씨 최소 게이트:
        - RAIN, SNOW, CLOUDY: 강수/실내 선호 [1.0, 0.0]
        - CLEAR: 맑음/청량 선호 [0.0, 1.0]
        """
        vec = np.zeros(2, dtype=np.float32)
        w = str(weather).upper()
        if w in ('', 'UNKNOWN', 'MISSING', 'NONE'):
            return vec
        if any(keyword in w for keyword in ['RAIN', 'SNOW', '비', '눈', '흐림', 'CLOUDY']):
            vec[0] = 1.0
            vec[1] = 0.1
        else:
            vec[0] = 0.1
            vec[1] = 1.0
        return vec

    def encode_time_slot(self, time_slot: str) -> np.ndarray:
        """
        [2~4] 영업 시간대 3칸:
        - MORNING (06~17): 주간 [1.0, 0.0, 0.0]
        - AFTERNOON / EVENING (17~22): 저녁 피크 [0.0, 1.0, 0.0]
        - NIGHT (22~06): 심야 [0.0, 0.0, 1.0]
        """
        vec = np.zeros(3, dtype=np.float32)
        t = str(time_slot).upper()
        if 'NIGHT' in t or '심야' in t:
            vec[2] = 1.0
        elif 'AFTERNOON' in t or 'EVENING' in t or '저녁' in t:
            vec[1] = 1.0
        else:
            vec[0] = 1.0
        return vec

    def encode_tastes(self, text_or_tags: Union[str, List[str]], category: str = '') -> np.ndarray:
        """
        [8~13] 6개 세부 취향 멀티-핫 인코딩
        """
        vec = np.zeros(6, dtype=np.float32)
        if isinstance(text_or_tags, list):
            target_str = ' '.join(text_or_tags).lower()
        else:
            target_str = str(text_or_tags).lower()

        cat = str(category).upper()

        for idx, keywords in self.TASTE_KEYWORDS.items():
            slot = idx - 8
            matched = any(kw.lower() in target_str for kw in keywords)
            if matched:
                vec[slot] = 1.0

        # 아무것도 매칭되지 않았을 때 카테고리 기반 기본값 활성화
        if vec.sum() == 0:
            if cat == 'PUB':
                vec[3] = 0.5  # 펍
                vec[5] = 0.5  # 포차
            else:
                vec[0] = 0.5  # 스페셜티
                vec[1] = 0.5  # 디저트

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    def encode_user_context(
        self,
        weather: str = 'CLEAR',
        time_slot: str = 'AFTERNOON',
        budget_tier: Union[int, str] = 2,
        preferred_tastes: Optional[List[str]] = None,
        prefer_local: bool = True,
        music_preference: str = 'CALM',
    ) -> np.ndarray:
        """유저 컨텍스트 -> 18차원 특성 벡터 생성"""
        vec = np.zeros(self.DIM, dtype=np.float32)

        # [0~1] 날씨
        vec[0:2] = self.encode_weather_gate(weather)

        # [2~4] 시간대
        vec[2:5] = self.encode_time_slot(time_slot)

        # [5~7] 예산
        vec[5:8] = self.encode_budget_tier(budget_tier)

        # [8~13] 취향
        vec[8:14] = self.encode_tastes(preferred_tastes or [])

        # [14~15] 개인 vs 체인
        if prefer_local:
            vec[14] = 1.0
            vec[15] = 0.0
        else:
            vec[14] = 0.0
            vec[15] = 1.0

        # [16~17] 음악 무드
        m = str(music_preference).upper()
        if any(k in m for k in ['CALM', 'LOFI', 'ACOUSTIC', 'JAZZ', '차분']):
            vec[16] = 1.0
            vec[17] = 0.1
        else:
            vec[16] = 0.1
            vec[17] = 1.0

        # L2 정규화
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def encode_venue(self, venue: Dict[str, Any]) -> np.ndarray:
        """매장 딕셔너리 정보 -> 18차원 특성 벡터 생성"""
        vec = np.zeros(self.DIM, dtype=np.float32)

        # [0~1] 날씨 적합도
        weather_affinity = venue.get('primary_weather_affinity', 'ALL')
        moods = venue.get('mood_tags', [])
        is_rain_vibe = any('비' in str(m) or '눈' in str(m) for m in moods) or weather_affinity in ('RAIN', 'SNOW')
        if is_rain_vibe:
            vec[0] = 1.0
            vec[1] = 0.2
        else:
            vec[0] = 0.2
            vec[1] = 1.0

        # [2~4] 시간대
        time_slot = venue.get('recommended_time_slot', 'AFTERNOON')
        vec[2:5] = self.encode_time_slot(time_slot)

        # [5~7] 가격 티어
        price_tier = venue.get('price_tier', 2)
        vec[5:8] = self.encode_budget_tier(price_tier)

        # [8~13] 세부 취향 키워드 결합
        venue_text = f"{venue.get('venue_name', '')} {venue.get('sub_category', '')} {' '.join(moods)}"
        vec[8:14] = self.encode_tastes(venue_text, venue.get('category', ''))

        # [14~15] 로컬 vs 프랜차이즈 (정규화된 store_type / 프랜차이즈여부 / 브랜드명 기준)
        if is_chain_venue(venue):
            vec[14] = 0.0
            vec[15] = 1.0
        else:
            vec[14] = 1.0
            vec[15] = 0.0

        # [16~17] 음악/공간 무드
        music = venue.get('music_mood', 'LOFI')
        if music in ('LOFI', 'ACOUSTIC', 'JAZZ', 'CHILL_HOP'):
            vec[16] = 1.0
            vec[17] = 0.1
        else:
            vec[16] = 0.1
            vec[17] = 1.0

        # L2 정규화
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


_encoder_instance: Optional[FeatureEncoder18D] = None


def get_feature_encoder() -> FeatureEncoder18D:
    global _encoder_instance
    if _encoder_instance is None:
        _encoder_instance = FeatureEncoder18D()
    return _encoder_instance
