import json
from django.test import TestCase, Client

from backend.venues.models.event import VenueEvent
from backend.venues.repositories.csv_lazy_repo import get_venue_repo


class TestStep4ApiViews(TestCase):
    def setUp(self):
        self.client = Client()
        self.repo = get_venue_repo()

    def test_venue_list_api_with_price_tier(self):
        """1. GET /api/venues?price_tier=10K_20K 검증"""
        response = self.client.get('/api/venues', {'gu': '강남구', 'price_tier': '10K_20K'})
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['gu'], '강남구')
        self.assertGreater(data['total_count'], 0)

        first_v = data['venues'][0]
        self.assertIn('venue_id', first_v)
        self.assertIn('venue_name', first_v)
        self.assertIn('price_tier', first_v)
        self.assertEqual(first_v['price_tier'], 2)
        self.assertIn('matched_menus_preview', first_v)
        self.assertIn('match_score', first_v)
        print(f"\n[Step 4 검증 1] GET /api/venues?price_tier=10K_20K: {data['total_count']}개 매장 조회 성공 (상태코드 200)")

    def test_venue_detail_api(self):
        """2. GET /api/venues/<venue_id> 상세 및 5개 탭 메뉴판 검증"""
        response = self.client.get('/api/venues/venue_seoul_0001')
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertTrue(data['success'])
        venue = data['venue']
        self.assertEqual(venue['venue_id'], 'venue_seoul_0001')
        self.assertIn('menu_tabs', venue)
        self.assertEqual(len(venue['menu_tabs']), 5, "메뉴판은 5개 카테고리 탭으로 구성되어야 합니다.")
        print(f"[Step 4 검증 2] GET /api/venues/venue_seoul_0001: 5개 탭({len(venue['menu_tabs'])}) 메뉴판 조회 성공 (상태코드 200)")

    def test_event_lifecycle_apis(self):
        """3. 점주 이벤트 발행 -> [참여할게요] -> 현장 [QR인증] 전체 라이프사이클 API 검증"""
        venue_id = 'venue_seoul_0001'
        owner_id = 'owner_kim'

        # [A] POST /api/owner/venues/<venue_id>/events (점주 이벤트 발행)
        create_payload = {
            'owner_id': owner_id,
            'title': '비 오는 날 아인슈페너 1+1',
            'description': '비 오는 날 한정 혜택',
            'discount_info': '아인슈페너 1+1',
            'total_quantity': 5,
            'duration_hours': 3,
        }
        res_create = self.client.post(
            f'/api/owner/venues/{venue_id}/events',
            data=json.dumps(create_payload),
            content_type='application/json',
        )
        self.assertEqual(res_create.status_code, 200)
        ev_data = res_create.json()
        event_id = ev_data['event_id']
        self.assertEqual(ev_data['remaining_quantity'], 5)

        # [B] POST /api/venues/events/<event_id>/intent (유저 [참여할게요])
        user_id = 'user_step4_tester'
        res_intent = self.client.post(
            f'/api/venues/events/{event_id}/intent',
            data=json.dumps({'user_id': user_id}),
            content_type='application/json',
        )
        self.assertEqual(res_intent.status_code, 200)
        intent_data = res_intent.json()
        qr_token = intent_data['qr_auth_code']
        # 재고는 그대로 5개 유지!
        self.assertEqual(intent_data['remaining_quantity'], 5)
        self.assertEqual(intent_data['intended_count'], 1)

        # [C] POST /api/venues/events/<event_id>/verify-qr (현장 QR 인증 -> 재고 -1 차감)
        res_verify = self.client.post(
            f'/api/venues/events/{event_id}/verify-qr',
            data=json.dumps({'user_id': user_id, 'qr_auth_code': qr_token}),
            content_type='application/json',
        )
        self.assertEqual(res_verify.status_code, 200)
        verify_data = res_verify.json()
        # 재고가 1 차감되어 4개가 되어야 함!
        self.assertEqual(verify_data['remaining_quantity'], 4)

        # [D] 당일 동일 계정 중복 인증 시도 -> 409 차단 검증
        res_dup = self.client.post(
            f'/api/venues/events/{event_id}/verify-qr',
            data=json.dumps({'user_id': user_id}),
            content_type='application/json',
        )
        self.assertEqual(res_dup.status_code, 409)
        self.assertEqual(res_dup.json()['error'], 'ALREADY_PARTICIPATED_TODAY')

        print(f"[Step 4 검증 3] 이벤트 발행/참여/QR인증/409중복차단 API 라이프사이클 성공")

    def test_my_summary_api(self):
        """4. GET /api/venues/my/summary 마이페이지 통합 데이터 검증"""
        user_id = 'user_my_summary_test'
        # 찜 하나 추가
        self.client.post(
            '/api/venues/venue_seoul_0001/bookmark',
            data=json.dumps({'user_id': user_id}),
            content_type='application/json',
        )

        response = self.client.get('/api/venues/my/summary', {'user_id': user_id})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['user_id'], user_id)

        summary = data['summary']
        self.assertIn('bookmarked_venues', summary)
        self.assertEqual(len(summary['bookmarked_venues']), 1)
        self.assertIn('reviews', summary)
        self.assertIn('participations', summary)
        self.assertIn('notifications', summary)
        print(f"[Step 4 검증 4] GET /api/venues/my/summary: 마이페이지 통합 4개 데이터셋 조회 성공 (상태코드 200)")

    def test_ai_concierge_api(self):
        """5. POST /api/venues/ai-concierge Gemini 3.8 Flash 컨시어지 API 검증"""
        payload = {
            'query': '비 오는데 강남역 근처에 2만원대로 파전에 막걸리 마실 로컬 술집 추천해줘',
            'gu': '강남구',
            'weather': 'RAIN',
            'time_slot': 'NIGHT',
            'category': 'PUB',
            'budget_tier': 2,
        }
        response = self.client.post(
            '/api/venues/ai-concierge',
            data=json.dumps(payload),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertTrue(data['success'])
        briefing = data['briefing']
        self.assertIn('concierge_title', briefing)
        self.assertIn('summary_briefing', briefing)
        self.assertIn('recommended_venue_name', briefing)
        self.assertIn('recommended_menu_combination', briefing)
        self.assertIn('model_used', briefing)
        print(f"[Step 4 검증 5] POST /api/venues/ai-concierge: 브리핑 생성 성공 (모델: {briefing['model_used']}, 매장: {briefing['recommended_venue_name']})")

    def test_alias_and_compatibility_endpoints(self):
        """6. 명세서 별칭 및 확장 엔드포인트 호환성 검증"""
        # A. GET /api/venues/search
        res_search = self.client.get('/api/venues/search', {'gu': '강남구'})
        self.assertEqual(res_search.status_code, 200)
        self.assertTrue(res_search.json()['success'])

        # B. GET /api/venues/<id>/menus
        res_menus = self.client.get('/api/venues/venue_seoul_0001/menus')
        self.assertEqual(res_menus.status_code, 200)
        self.assertTrue(res_menus.json()['success'])
        self.assertIn('recent_reviews', res_menus.json()['venue'])

        # C. POST /api/venues/concierge/recommend
        res_concierge = self.client.post(
            '/api/venues/concierge/recommend',
            data=json.dumps({'query': '따뜻한 카페', 'gu': '강남구'}),
            content_type='application/json',
        )
        self.assertEqual(res_concierge.status_code, 200)
        self.assertTrue(res_concierge.json()['success'])

        # D. GET /api/venues/events
        res_events = self.client.get('/api/venues/events', {'gu': '강남구'})
        self.assertEqual(res_events.status_code, 200)
        self.assertTrue(res_events.json()['success'])

        # E. GET /api/venues/notifications
        res_notis = self.client.get('/api/venues/notifications', {'user_id': 'test_user'})
        self.assertEqual(res_notis.status_code, 200)
        self.assertTrue(res_notis.json()['success'])

        # F. POST /api/venues/interactions
        res_inter = self.client.post(
            '/api/venues/interactions',
            data=json.dumps({'user_id': 'test_user', 'venue_id': 'venue_seoul_0001', 'action': 'CLICK'}),
            content_type='application/json',
        )
        self.assertEqual(res_inter.status_code, 200)
        self.assertTrue(res_inter.json()['success'])
        print("[Step 4 검증 6] 별칭 및 확장 엔드포인트 6종 정상 응답 확인 (상태코드 200)")
