import threading
from unittest.mock import MagicMock, patch

from django.test import Client, SimpleTestCase, override_settings

from backend.venues.dl_engine.feature_encoder_18d import FeatureEncoder18D
from backend.venues.repositories.csv_lazy_repo import (
    CsvLazyRepository,
    _extract_verified_research_menus,
    is_generic_template_menus,
)
from backend.venues.services.music_banner_service import get_venue_music_banner
from backend.venues.services.ranking_service import filter_and_rank_venues


class UnknownWeatherAndMenusTests(SimpleTestCase):
    def test_unknown_weather_is_not_encoded_as_sunshine(self):
        encoder = FeatureEncoder18D()
        self.assertEqual(encoder.encode_weather_gate('UNKNOWN').tolist(), [0.0, 0.0])
        self.assertEqual(encoder.encode_weather_gate('').tolist(), [0.0, 0.0])
        self.assertGreater(encoder.encode_weather_gate('CLEAR')[1], 0.9)
        self.assertGreater(encoder.encode_weather_gate('RAIN')[0], 0.9)

    @patch('backend.venues.services.music_banner_service.VenueOwnerBridge')
    def test_unknown_weather_does_not_claim_sunshine(self, bridge):
        bridge.objects.filter.return_value.first.return_value = None
        self.assertIsNone(get_venue_music_banner({'venue_id': 'v'}, 'UNKNOWN'))
        self.assertIsNotNone(get_venue_music_banner({'venue_id': 'v'}, 'CLEAR'))

    @override_settings(MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=False)
    @patch('backend.venues.views.list_view.check_and_send_nearby_event_promo')
    @patch('backend.venues.views.list_view.filter_and_rank_venues', return_value=([], 0))
    def test_missing_weather_is_forwarded_as_unknown(self, rank, promotion):
        response = Client().get('/api/venues')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rank.call_args.kwargs['weather'], 'UNKNOWN')

    def test_menu_templates_are_not_generated_or_mixed_into_live_data(self):
        repo = CsvLazyRepository.__new__(CsvLazyRepository)
        repo._gu_menus_loaded = {'구': True}
        repo._venue_menus_cache = {'v': [{'menu_id': 'source', 'venue_id': 'v', 'menu_name': '얼큰 해물 짬뽕 나베', 'price': 8000}]}
        repo._live_menus_by_vid = {}
        repo._live_menus_by_name = {}
        venue = {'venue_id': 'v', 'gu_name': '구', 'venue_name': '매장', 'category': 'PUB'}
        with patch('backend.venues.repositories.csv_lazy_repo._compute_menu_based_avg_price', return_value=(8000, '')):
            repo._enrich_venue_menus_and_budget(venue)
        self.assertEqual([m['menu_id'] for m in repo._venue_menus_cache['v']], ['source'])
        repo._live_menus_by_vid['v'] = [{'menu_id': 'real', 'menu_name': '확인 메뉴', 'price': 5000}]
        with patch('backend.venues.repositories.csv_lazy_repo._compute_menu_based_avg_price', return_value=(5000, '')):
            repo._enrich_venue_menus_and_budget(venue)
        self.assertEqual([m['menu_id'] for m in repo._venue_menus_cache['v']], ['real'])

    def test_unknown_weather_has_no_sunshine_score(self):
        repo = MagicMock()
        repo.get_venues_by_gu.return_value = [{'venue_id': 'v', 'venue_name': '매장',
                                                'primary_weather_affinity': 'CLEAR', 'rating': 4.0}]
        repo.get_preview_menus_by_venue.return_value = []
        tower = MagicMock()
        tower.compute_similarity.return_value = 0.0
        with patch('backend.venues.services.ranking_service.get_venue_repo', return_value=repo), \
             patch('backend.venues.services.ranking_service.get_feature_encoder'), \
             patch('backend.venues.services.ranking_service.get_micro_two_tower', return_value=tower), \
             patch('backend.venues.services.ranking_service.VenueStatusOverlay') as overlays, \
             patch('backend.venues.services.ranking_service.is_venue_open_now', return_value=True), \
             patch('backend.venues.services.ranking_service.get_visible_events_for_venue', return_value=[]), \
             patch('backend.venues.services.ranking_service.get_venue_music_banner', return_value=None):
            overlays.objects.filter.return_value = []
            unknown = filter_and_rank_venues('구', weather='UNKNOWN')[0]['match_score']
            clear = filter_and_rank_venues('구', weather='CLEAR')[0]['match_score']
        self.assertEqual(round(clear - unknown, 1), 10.0)

    def test_researched_menus_connect_and_generic_templates_are_hidden(self):
        parsed = _extract_verified_research_menus('v1', '소금 아인슈페너 | 바질토스트(대) | 미확인 | 5,000원 | 소금 아인슈페너')
        self.assertEqual([item['menu_name'] for item in parsed], ['소금 아인슈페너', '바질토스트(대)'])
        self.assertTrue(all(item['price'] == 0 and item['menu_source'] == 'RESEARCH' for item in parsed))

        generic_cafe = [
            {'menu_id': 'm1', 'menu_name': '아메리카노 (고소한 블렌드)', 'price': 4500},
            {'menu_id': 'm2', 'menu_name': '카페 라떼', 'price': 5000},
            {'menu_id': 'm3', 'menu_name': '바닐라빈 라떼', 'price': 5800},
        ]
        generic_pub = [
            {'menu_id': 'p1', 'menu_name': '얼큰 해물 짬뽕 나베', 'price': 21000},
            {'menu_id': 'p2', 'menu_name': '수제 모듬 꼬치구이 (6종)', 'price': 18000},
            {'menu_id': 'p3', 'menu_name': '바삭 먹태구이 & 청양마요 소스', 'price': 15000},
        ]
        brand_cafe = [
            {'menu_id': 'b1', 'menu_name': '메가리카노 (32oz)', 'price': 3000},
            {'menu_id': 'b2', 'menu_name': '아메리카노 (HOT/ICE)', 'price': 2000},
            {'menu_id': 'b3', 'menu_name': '큐브라떼', 'price': 4200},
        ]
        self.assertTrue(is_generic_template_menus(generic_cafe))
        self.assertTrue(is_generic_template_menus(generic_pub))
        self.assertFalse(is_generic_template_menus(brand_cafe))
        self.assertFalse(is_generic_template_menus(parsed))

        repo = CsvLazyRepository.__new__(CsvLazyRepository)
        repo._lock = threading.RLock()
        repo._gu_menus_loaded = {'강남구': True}
        repo._live_menus_by_vid = {}
        repo._live_menus_by_name = {}
        repo._venue_map = {}
        repo._venue_menus_cache = {'v_gen': generic_cafe, 'v_res': generic_cafe, 'v_brand': brand_cafe}

        venue_gen = {'venue_id': 'v_gen', 'gu_name': '강남구', 'venue_name': '미확인카페', 'category': 'CAFE', 'avg_price': 8000, 'price_tier': 1}
        venue_res = {
            'venue_id': 'v_res', 'gu_name': '강남구', 'venue_name': '조사카페', 'category': 'CAFE', 'avg_price': 9000, 'price_tier': 1,
            'research': {'representative_menus': '말차 크림 라떼 | 휘낭시에'},
        }
        venue_brand = {'venue_id': 'v_brand', 'gu_name': '강남구', 'venue_name': '메가MGC', 'category': 'CAFE', 'avg_price': 3500, 'price_tier': 1}
        for v in (venue_gen, venue_res, venue_brand):
            repo._venue_map[v['venue_id']] = v
            repo._enrich_venue_menus_and_budget(v)

        self.assertEqual(repo.get_preview_menus_by_venue(venue_gen), [])
        self.assertEqual([m['menu_name'] for m in repo.get_preview_menus_by_venue(venue_res)], ['말차 크림 라떼', '휘낭시에'])
        self.assertEqual(venue_res['avg_price'], 9000)
        self.assertEqual([m['menu_name'] for m in repo.get_preview_menus_by_venue(venue_brand)], ['메가리카노 (32oz)', '아메리카노 (HOT/ICE)', '큐브라떼'])

    def test_ranking_prioritizes_verified_unique_menus_then_brand_then_unverified(self):
        repo = MagicMock()
        v_unverified = {'venue_id': 'v0', 'venue_name': '미확인', 'primary_weather_affinity': 'CLEAR', 'rating': 4.9}
        v_brand = {'venue_id': 'v1', 'venue_name': '브랜드', 'primary_weather_affinity': 'CLEAR', 'rating': 4.2}
        v_research = {'venue_id': 'v2', 'venue_name': '조사매장', 'primary_weather_affinity': 'CLEAR', 'rating': 4.2}
        repo.get_venues_by_gu.return_value = [v_unverified, v_brand, v_research]
        repo.get_preview_menus_by_venue.side_effect = lambda venue, **kw: {
            'v0': [],
            'v1': [{'menu_id': 'b1', 'menu_name': '메가리카노', 'price': 3000}],
            'v2': [{'menu_id': 'RM_v2_1', 'menu_name': '소금 아인슈페너', 'price': 0, 'menu_source': 'RESEARCH'}],
        }[venue['venue_id']]
        tower = MagicMock()
        tower.compute_similarity.return_value = 0.0
        with patch('backend.venues.services.ranking_service.get_venue_repo', return_value=repo), \
             patch('backend.venues.services.ranking_service.get_feature_encoder'), \
             patch('backend.venues.services.ranking_service.get_micro_two_tower', return_value=tower), \
             patch('backend.venues.services.ranking_service.VenueStatusOverlay') as overlays, \
             patch('backend.venues.services.ranking_service.is_venue_open_now', return_value=True), \
             patch('backend.venues.services.ranking_service.get_visible_events_for_venue', return_value=[]), \
             patch('backend.venues.services.ranking_service.get_venue_music_banner', return_value=None):
            overlays.objects.filter.return_value = []
            ranked = filter_and_rank_venues('강남구', weather='CLEAR')
        self.assertEqual([item['venue_id'] for item in ranked], ['v2', 'v1', 'v0'])
        self.assertTrue(ranked[0]['has_verified_menus'])
        self.assertFalse(ranked[2]['has_verified_menus'])
