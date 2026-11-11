import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from backend.venues.models.status_overlay import VenueStatusOverlay


@override_settings(LOCAL_DEMO=True, MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=False)
class LocalCatalogDetailTests(TestCase):
    venue_id = 'VN_IC_000038'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'preview.json'
        self.venue = {
            'venue_id': self.venue_id, 'name': '인천 검증 매장', 'address': '인천 연수구 검증로 1',
            'admin_dong': '송도2동', 'venue_category': 'PUB', 'is_public': True,
            'research': {'description': '방문 후기 설명', 'sources': ['https://example.com/review'],
                         'limitations': '현재 운영 상태 미확인', 'checked_on': '2026-11-10'},
        }
        self.write_snapshot()
        self.env = patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repository = patch('backend.venues.views.detail_view.get_venue_repo')
        self.mock_repository = self.repository.start()
        self.mock_repository.return_value.get_venue_by_id.return_value = None
        self.addCleanup(self.repository.stop)
        self.client = Client()

    def write_snapshot(self, *, venues=None, **extra):
        self.path.write_text(json.dumps({'version': 'local-test', 'preview_only': True,
                                        'venues': [self.venue] if venues is None else venues,
                                        'menus': [], **extra}, ensure_ascii=False), encoding='utf-8')

    def detail(self):
        return self.client.get(f'/api/venues/{self.venue_id}')

    def test_same_public_catalog_id_has_read_only_detail_without_invented_facts(self):
        response = self.detail()
        self.assertEqual(response.status_code, 200)
        result = response.json()['venue']
        self.assertEqual((result['venue_id'], result['venue_name'], result['road_address']),
                         (self.venue_id, self.venue['name'], self.venue['address']))
        self.assertTrue(result['curated_listing']['preview_only'])
        self.assertEqual(result['research']['sources'], ['https://example.com/review'])
        self.assertNotIn('avg_price', result)
        self.assertNotIn('is_open_now', result)
        self.assertNotIn('open_hours', result)

    def test_unpublished_duplicate_and_hidden_entries_are_not_returned(self):
        # A stale CSV entry must not override a hidden snapshot record.
        self.mock_repository.return_value.get_venue_by_id.return_value = {'venue_id': self.venue_id}
        self.write_snapshot(venues=[{**self.venue, 'is_public': False}])
        self.assertEqual(self.detail().status_code, 404)
        self.write_snapshot(venues=[self.venue, self.venue])
        self.assertEqual(self.detail().status_code, 404)
        self.write_snapshot()
        VenueStatusOverlay.objects.create(venue_id=self.venue_id, status='CLOSED_HIDDEN')
        self.assertEqual(self.detail().status_code, 404)

    def test_snapshot_is_reread_after_update_and_requires_explicit_preview_marker(self):
        self.assertEqual(self.detail().json()['venue']['venue_name'], '인천 검증 매장')
        self.venue['name'] = '변경된 검증 매장'
        self.write_snapshot()
        self.assertEqual(self.detail().json()['venue']['venue_name'], '변경된 검증 매장')
        self.path.write_text(json.dumps({'version': 'local-test', 'venues': [self.venue], 'menus': []}), encoding='utf-8')
        self.assertEqual(self.detail().status_code, 404)

    @override_settings(LOCAL_DEMO=False)
    def test_non_demo_never_reads_local_snapshot(self):
        self.assertEqual(self.detail().status_code, 404)
