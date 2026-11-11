from unittest.mock import MagicMock, patch

from django.test import Client, SimpleTestCase, override_settings

from backend.venues.dl_engine.feature_encoder_18d import FeatureEncoder18D
from backend.venues.repositories.csv_lazy_repo import CsvLazyRepository
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
