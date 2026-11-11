import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from backend.accounts.models import MemberProfile
from backend.recommendation.models import Catalog, Family, Job, Playlist
from backend.workspaces.models import ServiceMembership, WorkspaceClaim
from backend.workspaces.music import public_applied_playlist
from backend.workspaces.tests.test_catalog_claims import CatalogRepo
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG


@override_settings(**TEST_CONFIG)
class CatalogMusicBindingTests(Helpers, TestCase):
    def setUp(self):
        self.repo = CatalogRepo()
        self.repo_patch = patch('backend.venues.repositories.csv_lazy_repo.get_venue_repo', return_value=self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.owner = self.user('music-catalog-owner')
        self.owner_client = self.client_for(self.owner)
        self.consumer = self.client_for(self.user('music-catalog-consumer', 'CONSUMER'))
        _, self.reviewer = self.reviewer()

    def test_catalog_approval_and_current_owner_playlist_are_required(self):
        venue_id = 'VN_IC_000002'
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:' + venue_id))
        self.assertIsNone(public_applied_playlist(venue_id))
        claim = self.approve(self.reviewer, claim)
        workspace = claim['approved_workspace_id']
        profile = MemberProfile.objects.get(user=self.owner)
        catalog = Catalog.objects.create(version='catalog-music', tracks=[], sha256='x', source_ref='test', source_sha256='x')
        family = Family.objects.create(owner_id=str(profile.owner_id), options={}, policy_version='test', mapping_version='test')
        job = Job.objects.create(family=family, catalog=catalog, request_id='catalog-music', status='SUCCEEDED', input={})
        playlist = Playlist.objects.create(family=family, job=job, snapshot={'summary': '점주 선곡'})
        family.approved_playlist_id = playlist.pk
        family.save(update_fields=['approved_playlist_id'])
        self.data(self.call(self.owner_client, f'/workspaces/{workspace}/music',
                            {'action': 'APPLY', 'playlist_id': playlist.pk}, method='patch', version=1))

        self.assertEqual(public_applied_playlist(venue_id), {'id': playlist.pk, 'name': '점주 선곡'})
        self.repo.get_menus_by_venue = lambda _venue_id: []
        with patch('backend.venues.views.detail_view.get_venue_repo', return_value=self.repo), \
             patch('backend.venues.views.detail_view.is_venue_open_now', return_value=None), \
             patch('backend.venues.views.detail_view.get_venue_music_banner', return_value=None), \
             patch('backend.venues.views.detail_view.get_visible_events_for_venue', return_value=[]):
            detail = self.consumer.get(f'/api/venues/{venue_id}')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['venue']['applied_playlist'], {'id': playlist.pk, 'name': '점주 선곡'})
        self.assertIsNone(public_applied_playlist('VN_SL_000001'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.json'
            path.write_text(json.dumps({'version': 'test', 'preview_only': True, 'menus': [], 'venues': [{
                'venue_id': venue_id, 'name': '매장', 'address': '인천 연수구', 'admin_dong': '송도동',
                'venue_category': 'PUB', 'is_public': True,
            }]}), encoding='utf-8')
            with override_settings(LOCAL_DEMO=True), patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': str(path)}):
                preview = self.consumer.get(f'/api/venues/{venue_id}')
                self.assertEqual(preview.status_code, 200)
                self.assertTrue(preview.json()['venue']['curated_listing']['preview_only'])
                self.assertEqual(preview.json()['venue']['applied_playlist'], {'id': playlist.pk, 'name': '점주 선곡'})
                membership = ServiceMembership.objects.get(user=self.owner, kind='BUSINESS')
                membership.status = 'SUSPENDED'
                membership.save(update_fields=['status'])
                self.assertIsNone(public_applied_playlist(venue_id))
                self.assertIsNone(self.consumer.get(f'/api/venues/{venue_id}').json()['venue']['applied_playlist'])
                inactive = self.user('inactive-preview-consumer', 'CONSUMER')
                ServiceMembership.objects.filter(user=inactive, kind='CONSUMER').update(status='SUSPENDED')
                self.assertEqual(self.client_for(inactive).get(f'/api/venues/{venue_id}').status_code, 403)
                self.consumer.logout()
                self.assertEqual(self.consumer.get(f'/api/venues/{venue_id}').status_code, 401)
                membership.status = 'ACTIVE'
                membership.save(update_fields=['status'])
        WorkspaceClaim.objects.filter(pk=claim['id']).update(fixture_id='demo-linked-venue-000001')
        self.assertIsNone(public_applied_playlist(venue_id))

    def test_demo_playlist_hidden_when_owner_membership_is_suspended(self):
        claim = self.approve(self.reviewer, self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002')))
        self.data(self.call(self.owner_client, f"/workspaces/{claim['approved_workspace_id']}/music",
                            {'action': 'APPLY', 'playlist_id': 'demo-calm'}, method='patch', version=1))
        self.assertIsNotNone(public_applied_playlist('VN_IC_000002'))
        ServiceMembership.objects.filter(user=self.owner, kind='BUSINESS').update(status='SUSPENDED')
        self.assertIsNone(public_applied_playlist('VN_IC_000002'))
