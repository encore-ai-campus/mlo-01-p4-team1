from threading import RLock
from io import StringIO
from copy import deepcopy
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from backend.workspaces.models import Workspace
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG
from backend.venues.repositories.csv_lazy_repo import CsvLazyRepository
from backend.venues.models.status_overlay import VenueStatusOverlay


class CatalogRepo:
    def __init__(self):
        self.venues = {
            'VN_SL_000001': {'venue_id': 'VN_SL_000001', 'venue_name': '첫째 매장', 'road_address': '서울 강남구 첫째길'},
            'VN_IC_000002': {'venue_id': 'VN_IC_000002', 'venue_name': '둘째 매장', 'road_address': '인천 연수구 둘째길'},
        }

    def get_venue_by_id(self, venue_id):
        return self.venues.get(venue_id)

    def get_all_available_gus(self):
        return ['강남구', '연수구']

    def get_venues_by_gu(self, region):
        return [self.venues['VN_SL_000001' if region == '강남구' else 'VN_IC_000002']]

    def source_region_available(self, region):
        return region in self.get_all_available_gus()

    def get_source_venues_by_region(self, region):
        return self.get_venues_by_gu(region)

    def get_source_venue_by_id(self, venue_id):
        return self.venues.get(venue_id)


class CatalogColdLookupTests(TestCase):
    def test_uncached_other_region_id_is_resolved_without_prior_list_query(self):
        repo = CsvLazyRepository.__new__(CsvLazyRepository)
        repo._lock = RLock()
        repo._gu_venues_cache = {'강남구': []}
        repo._venue_map = {}
        repo._fallback_venues_loaded = True
        repo._venue_region_index = {'VN_IC_000002': '연수구'}
        target = {'venue_id': 'VN_IC_000002', 'venue_name': '인천 매장'}

        def load(region):
            if region == '연수구':
                repo._venue_map[target['venue_id']] = target
            repo._gu_venues_cache[region] = []
            return []

        with patch.object(repo, 'get_all_available_gus', return_value=['강남구', '연수구']), \
             patch.object(repo, 'get_venues_by_gu', side_effect=load):
            self.assertEqual(repo.get_venue_by_id('VN_IC_000002'), target)

    def test_index_resolves_incheon_junggu_without_loading_seoul_junggu(self):
        repo = CsvLazyRepository.__new__(CsvLazyRepository)
        repo._lock = RLock()
        repo._gu_venues_cache = {'강남구': []}
        repo._venue_map = {}
        repo._fallback_venues_loaded = True
        repo._venue_region_index = None
        target = {'venue_id': 'VN_IC_000003', 'venue_name': '인천 매장'}
        path = MagicMock()
        path.name = '중구_venues.csv'
        path.open.return_value.__enter__.return_value = StringIO('venue_id,매장명\nVN_IC_000003,인천 매장\n')
        def load(region):
            self.assertEqual(region, '인천 중구')
            repo._venue_map[target['venue_id']] = target
            return [target]

        with patch.object(repo, 'get_all_available_gus', return_value=['인천 중구']), \
             patch.object(repo, '_find_region_csv', return_value=path), \
             patch.object(repo, 'get_venues_by_gu', side_effect=load):
            self.assertEqual(repo.get_venue_by_id('VN_IC_000003'), target)


@override_settings(**TEST_CONFIG)
class CatalogClaimTests(Helpers, TestCase):
    def setUp(self):
        self.repo = CatalogRepo()
        self.mock_repo = patch('backend.venues.repositories.csv_lazy_repo.get_venue_repo', return_value=self.repo)
        self.mock_repo.start()
        self.addCleanup(self.mock_repo.stop)
        self.owner = self.user('catalog-owner')
        self.owner_client = self.client_for(self.owner)
        _, self.reviewer_client = self.reviewer()

    def test_business_search_is_restricted_and_region_scoped(self):
        url = '/api/platform/v1/claim-venues?region=연수구&q=매장'
        self.assertEqual(Client().get(url).status_code, 401)
        consumer = self.client_for(self.user('consumer', 'CONSUMER'))
        self.assertEqual(consumer.get(url).status_code, 409)
        response = self.owner_client.get(url)
        self.response_matches(response, '/claim-venues', 'get')
        self.assertEqual(self.data(response)['items'], [self.repo.venues['VN_IC_000002']])
        self.assertEqual(self.owner_client.get('/api/platform/v1/claim-venues?region=연수구&q=매').status_code, 400)

    def test_search_hides_sample_when_region_source_is_absent(self):
        with patch.object(self.repo, 'source_region_available', return_value=False), \
             patch.object(self.repo, 'get_source_venues_by_region', wraps=self.repo.get_source_venues_by_region) as lookup:
            response = self.owner_client.get('/api/platform/v1/claim-venues?region=강남구&q=매장')
        self.assertEqual(self.data(response)['items'], [])
        lookup.assert_not_called()
        # The explicit synthetic fixture still supports the existing demo.
        claim = self.claim(self.owner_client, 'demo-linked-venue-000001')
        self.assertEqual(claim['display_name'], '첫째 매장')

    def test_search_returns_error_when_source_cannot_be_read(self):
        with patch.object(self.repo, 'get_source_venues_by_region', side_effect=OSError('source unavailable')):
            response = self.owner_client.get('/api/platform/v1/claim-venues?region=강남구&q=매장')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'VENUE_REFRESH_FAILED')

    def test_catalog_claim_uses_server_data_and_requires_review(self):
        claim = self.claim(self.owner_client, 'catalog:VN_IC_000002')
        self.assertEqual((claim['display_name'], claim['address']), ('둘째 매장', '인천 연수구 둘째길'))
        self.assertIn('실제 국세청 확인이 아닙니다', claim['notice'])
        claim = self.submit(self.owner_client, claim)
        self.assertEqual(Workspace.objects.count(), 0)
        claim = self.approve(self.reviewer_client, claim)
        self.assertEqual(claim['state'], 'APPROVED')
        self.assertEqual(Workspace.objects.get(pk=claim['approved_workspace_id']).location_key, 'catalog:VN_IC_000002')

    def test_duplicate_and_legacy_alias_are_blocked(self):
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'demo-linked-venue-000001'))
        self.approve(self.reviewer_client, claim)
        other = self.client_for(self.user('catalog-other'))
        duplicate = self.submit(other, self.claim(other, 'catalog:VN_SL_000001'))
        response = self.call(self.reviewer_client, f"/admin/workspace-claims/{duplicate['id']}/decisions",
                             {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'}, version=duplicate['revision'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Workspace.objects.count(), 1)

    def test_missing_and_invalid_catalog_ids_are_rejected(self):
        body = {'display_name': '입력명', 'address': '입력 주소', 'applicant_relation': 'DELEGATE'}
        for fixture in ['catalog:VN_MISSING', 'catalog:../bad']:
            response = self.call(self.owner_client, '/workspace-claims', {**body, 'fixture_id': fixture})
            self.assertIn(response.status_code, (400, 409))
        self.assertEqual(Workspace.objects.count(), 0)

    def test_deleted_venue_cannot_be_approved(self):
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002'))
        del self.repo.venues['VN_IC_000002']
        response = self.call(self.reviewer_client, f"/admin/workspace-claims/{claim['id']}/decisions",
                             {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'}, version=claim['revision'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Workspace.objects.count(), 0)

    def test_changed_catalog_address_must_be_reviewed_and_resubmitted(self):
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002'))
        self.repo.venues['VN_IC_000002']['road_address'] = '인천 연수구 바뀐길'
        path = f"/admin/workspace-claims/{claim['id']}/decisions"
        response = self.call(self.reviewer_client, path,
                             {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'},
                             version=claim['revision'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'VENUE_CHANGED')
        self.assertEqual(Workspace.objects.count(), 0)

        returned = self.data(self.call(self.reviewer_client, path,
                             {'decision': 'REQUEST_MORE', 'reason_code': 'CORRECTION_REQUIRED'},
                             version=claim['revision']))['claim']
        edited = self.data(self.call(self.owner_client, f"/workspace-claims/{claim['id']}",
                           {'display_name': '예전 이름', 'address': '예전 주소',
                            'applicant_relation': 'DELEGATE', 'fixture_id': 'catalog:VN_IC_000002'},
                           method='patch', version=returned['revision']))
        self.assertEqual(edited['address'], '인천 연수구 바뀐길')
        approved = self.approve(self.reviewer_client, self.submit(self.owner_client, edited))
        self.assertEqual(approved['state'], 'APPROVED')
        self.assertEqual(Workspace.objects.count(), 1)

    def test_approval_reads_source_instead_of_stale_cached_catalog(self):
        class CacheAwareRepo(CatalogRepo):
            def __init__(self):
                super().__init__()
                self.source = deepcopy(self.venues)
                self.source_calls = 0

            def get_source_venue_by_id(self, venue_id):
                self.source_calls += 1
                return deepcopy(self.source.get(venue_id))

        repo = CacheAwareRepo()
        with patch('backend.venues.repositories.csv_lazy_repo.get_venue_repo', return_value=repo):
            claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002'))
            repo.source['VN_IC_000002']['road_address'] = '인천 연수구 실제 변경된 주소'
            self.assertNotEqual(repo.get_venue_by_id('VN_IC_000002')['road_address'], repo.source['VN_IC_000002']['road_address'])
            response = self.call(self.reviewer_client, f"/admin/workspace-claims/{claim['id']}/decisions",
                                 {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'}, version=claim['revision'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'VENUE_CHANGED')
        self.assertEqual(repo.source_calls, 2)
        self.assertEqual(Workspace.objects.count(), 0)

    def test_create_and_edit_use_source_not_stale_or_sample_cache(self):
        class CacheAwareRepo(CatalogRepo):
            def __init__(self):
                super().__init__()
                self.source = deepcopy(self.venues)

            def get_source_venue_by_id(self, venue_id):
                return deepcopy(self.source.get(venue_id))

        repo = CacheAwareRepo()
        repo.source['VN_IC_000002']['venue_name'] = '원본 매장'
        repo.source['VN_IC_000002']['road_address'] = '인천 연수구 원본 주소'
        with patch('backend.venues.repositories.csv_lazy_repo.get_venue_repo', return_value=repo):
            claim = self.claim(self.owner_client, 'catalog:VN_IC_000002')
            self.assertEqual((claim['display_name'], claim['address']), ('원본 매장', '인천 연수구 원본 주소'))
            repo.source['VN_IC_000002']['road_address'] = '인천 연수구 새 주소'
            edited = self.data(self.call(self.owner_client, f"/workspace-claims/{claim['id']}",
                               {'display_name': '입력명', 'address': '입력 주소',
                                'applicant_relation': 'DELEGATE', 'fixture_id': 'catalog:VN_IC_000002'},
                               method='patch', version=claim['revision']))
            self.assertEqual(edited['address'], '인천 연수구 새 주소')

    def test_sample_only_venue_cannot_be_submitted_as_catalog_claim(self):
        with patch.object(self.repo, 'get_source_venue_by_id', return_value=None):
            response = self.call(self.owner_client, '/workspace-claims',
                                 {'display_name': '샘플 매장', 'address': '샘플 주소',
                                  'applicant_relation': 'DELEGATE', 'fixture_id': 'catalog:VN_SL_000001'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'VENUE_UNAVAILABLE')

    def test_catalog_refresh_failure_does_not_approve_stale_data(self):
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002'))
        with patch.object(self.repo, 'get_source_venue_by_id', side_effect=OSError('source unavailable')):
            response = self.call(self.reviewer_client, f"/admin/workspace-claims/{claim['id']}/decisions",
                                 {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'}, version=claim['revision'])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'VENUE_REFRESH_FAILED')
        self.assertEqual(Workspace.objects.count(), 0)

    def test_closed_hidden_venue_cannot_be_searched_or_claimed(self):
        VenueStatusOverlay.objects.create(venue_id='VN_IC_000002', status='CLOSED_HIDDEN')
        response = self.owner_client.get('/api/platform/v1/claim-venues?region=연수구&q=매장')
        self.assertEqual(self.data(response)['items'], [])
        response = self.call(self.owner_client, '/workspace-claims',
                             {'display_name': '매장', 'address': '주소', 'applicant_relation': 'DELEGATE',
                              'fixture_id': 'catalog:VN_IC_000002'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'VENUE_UNAVAILABLE')
        self.assertEqual(Workspace.objects.count(), 0)

    def test_venue_hidden_after_submission_cannot_be_approved(self):
        claim = self.submit(self.owner_client, self.claim(self.owner_client, 'catalog:VN_IC_000002'))
        VenueStatusOverlay.objects.create(venue_id='VN_IC_000002', status='CLOSED_HIDDEN')
        response = self.call(self.reviewer_client, f"/admin/workspace-claims/{claim['id']}/decisions",
                             {'decision': 'APPROVE', 'reason_code': 'DEMO_SCENARIO_CONFIRMED'}, version=claim['revision'])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'VENUE_UNAVAILABLE')
        self.assertEqual(Workspace.objects.count(), 0)
