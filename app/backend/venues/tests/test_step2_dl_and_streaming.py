import time
import numpy as np
from django.test import TestCase

from backend.venues.dl_engine.feature_encoder_18d import get_feature_encoder
from backend.venues.dl_engine.micro_two_tower import get_micro_two_tower
from backend.venues.dl_engine.realtime_user_memory import get_user_memory
from backend.venues.streaming.async_kafka_dispatcher import enqueue_behavior_event, get_async_dispatcher
from backend.venues.repositories.csv_lazy_repo import get_venue_repo


class TestStep2TwoTowerAndStreaming(TestCase):
    def setUp(self):
        self.encoder = get_feature_encoder()
        self.two_tower = get_micro_two_tower()
        self.memory = get_user_memory()
        self.dispatcher = get_async_dispatcher()
        self.repo = get_venue_repo()
        self.memory.clear()

    def test_non_blocking_enqueue_speed(self):
        """enqueue_behavior_event 호출 시간이 0.005초(5ms) 미만인지 검증"""
        user_id = 'test_user_001'
        venue_id = 'venue_seoul_0001'

        t0 = time.perf_counter()
        success = enqueue_behavior_event(
            user_id=user_id,
            venue_id=venue_id,
            behavior_type='EVENT_INTENT',
            extra_data={'button': '참여할게요'},
        )
        t1 = time.perf_counter()
        elapsed = t1 - t0

        self.assertTrue(success)
        self.assertLess(elapsed, 0.005, f"호출 시간({elapsed*1000:.3f}ms)이 5ms를 초과하여 블로킹되었습니다.")
        print(f"\n[Step 2 검증 1] enqueue_behavior_event 호출 시간: {elapsed*1000:.4f} ms (< 5ms 기준 PASS)")

    def test_two_tower_similarity_increase_on_rainy_pocha_intent(self):
        """
        비 오는 날 포차/전집 매장에 대해 유저가 [참여할게요](w=5.0)를 눌렀을 때,
        유저 16D 취향 벡터와 해당 포차 매장 벡터 간 Two-Tower 코사인 유사도 점수 즉각 상승 검증
        """
        user_id = 'rainy_user_777'

        # 1. 비 오는 날 전집/포차 가상 매장 생성
        pocha_venue = {
            'venue_id': 'venue_rainy_pocha_999',
            'venue_name': '종로 빈대떡과 막걸리 골목집',
            'category': 'PUB',
            'sub_category': '실내포차',
            'price_tier': 2,
            'primary_weather_affinity': 'RAIN',
            'recommended_time_slot': 'NIGHT',
            'mood_tags': ['비오는날감성', '포차', '전집', '막걸리', '국물'],
            'music_mood': 'ACOUSTIC',
        }

        # 2. 매장 18D 인코딩 및 16D Two-Tower 매장 임베딩 산출
        venue_18d = self.encoder.encode_venue(pocha_venue)
        self.assertEqual(venue_18d.shape, (18,))
        # L2 정규화 후 비 오는 날 게이트([0])가 맑은 날 게이트([1])보다 훨씬 크고, 포차/전집 취향([13])이 활성화됨을 확인
        self.assertGreater(venue_18d[0], venue_18d[1] * 2)
        self.assertGreater(venue_18d[13], 0.0)

        venue_16d = self.two_tower.forward_venue(venue_18d)
        self.assertEqual(venue_16d.shape, (16,))

        # 3. 유저 초기 벡터 설정 (맑은 날 브런치 카페를 선호하는 상태로 초기화)
        initial_user_context = self.encoder.encode_user_context(
            weather='CLEAR',
            time_slot='MORNING',
            budget_tier=1,
            preferred_tastes=['스페셜티', '카공'],
        )
        initial_user_16d = self.two_tower.forward_user(initial_user_context)
        self.memory.set_user_vector(user_id, initial_user_16d)

        # 4. 행동 전 코사인 유사도 측정
        sim_before = self.two_tower.compute_similarity(initial_user_16d, venue_16d)

        # 5. 유저가 비 오는 날 해당 매장에 [참여할게요] (w=5.0) 클릭!
        updated_user_16d = self.memory.update_user_vector(
            user_id=user_id,
            behavior_type='EVENT_INTENT',
            venue_embedding=venue_16d,
            weight_override=5.0,
        )

        # 6. 행동 후 코사인 유사도 측정
        sim_after = self.two_tower.compute_similarity(updated_user_16d, venue_16d)
        delta_sim = sim_after - sim_before

        print(f"[Step 2 검증 2] [참여할게요] (w=5.0) 클릭 전후 Two-Tower 코사인 유사도 변화:")
        print(f"  - 이전 유사도: {sim_before:.6f}")
        print(f"  - 이후 유사도: {sim_after:.6f}")
        print(f"  - 상승 폭 (Delta): +{delta_sim:.6f}")

        # 유사도가 유의미하게 대폭 상승해야 함
        self.assertGreater(sim_after, sim_before, "행동 후 유사도가 상승해야 합니다.")
        self.assertGreater(delta_sim, 0.15, f"가중치 5.0 반영 후 유사도 상승폭({delta_sim:.4f})이 +0.15 이상이어야 합니다.")
