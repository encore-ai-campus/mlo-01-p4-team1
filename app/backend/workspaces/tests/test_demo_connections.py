from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.workspaces.models import BusinessCheckJob, ClaimDecision, Workspace, WorkspaceMembership
from backend.workspaces.tests.test_catalog_claims import CatalogRepo
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG


@override_settings(**TEST_CONFIG)
class DemoConnectionTests(Helpers, TestCase):
    def setUp(self):
        self.repo = CatalogRepo()
        self.repo_patch = patch('backend.venues.repositories.csv_lazy_repo.get_venue_repo', return_value=self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.owner = self.user('demo-connect-owner')
        self.client_owner = self.client_for(self.owner)
        self.body = {'display_name': '입력한 이름', 'address': '입력한 주소',
                     'applicant_relation': 'REPRESENTATIVE', 'fixture_id': 'catalog:VN_IC_000002'}

    def test_connect_immediately_without_business_check_or_reviewer(self):
        response = self.call(self.client_owner, '/workspace-connections', self.body, key='a' * 32)
        self.response_matches(response, '/workspace-connections', 'post')
        result = self.data(response, 201)
        self.assertEqual(result['state'], 'APPROVED')
        self.assertEqual(result['review_context']['authority_evidence_kind'], 'UNVERIFIED')
        self.assertEqual((result['display_name'], result['address']), ('둘째 매장', '인천 연수구 둘째길'))
        self.assertIn('소유 확인 없이', result['notice'])
        self.assertEqual(BusinessCheckJob.objects.count(), 0)
        self.assertEqual(ClaimDecision.objects.count(), 0)
        self.assertTrue(WorkspaceMembership.objects.filter(workspace_id=result['approved_workspace_id'],
                         user=self.owner, role='OWNER', status='ACTIVE').exists())
        replay = self.call(self.client_owner, '/workspace-connections', self.body, key='a' * 32)
        self.assertEqual(self.data(replay, 201)['approved_workspace_id'], result['approved_workspace_id'])
        self.assertEqual(Workspace.objects.count(), 1)

    def test_existing_workspace_cannot_be_taken_over(self):
        result = self.data(self.call(self.client_owner, '/workspace-connections', self.body), 201)
        other = self.client_for(self.user('demo-connect-other'))
        response = self.call(other, '/workspace-connections', self.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'WORKSPACE_ALREADY_LINKED')
        self.assertEqual(other.get(f"/api/platform/v1/workspaces/{result['approved_workspace_id']}/music").status_code, 404)
        self.assertEqual(Workspace.objects.count(), 1)

    def test_legacy_demo_and_catalog_alias_cannot_claim_the_same_venue_twice(self):
        legacy = {**self.body, 'fixture_id': 'demo-linked-venue-000001'}
        self.data(self.call(self.client_owner, '/workspace-connections', legacy), 201)
        other = self.client_for(self.user('demo-connect-alias'))
        response = self.call(other, '/workspace-connections', {**self.body, 'fixture_id': 'catalog:VN_SL_000001'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Workspace.objects.count(), 1)

    def test_login_and_business_type_are_required(self):
        response = Client().post('/api/platform/v1/workspace-connections', self.body, content_type='application/json',
                                 HTTP_IDEMPOTENCY_KEY='b' * 32)
        self.assertEqual(response.status_code, 401)
        consumer = self.client_for(self.user('demo-connect-consumer', 'CONSUMER'))
        self.assertEqual(self.call(consumer, '/workspace-connections', self.body).status_code, 409)

    def test_unavailable_or_hidden_venue_cannot_be_connected(self):
        VenueStatusOverlay.objects.create(venue_id='VN_IC_000002', status='CLOSED_HIDDEN')
        self.assertEqual(self.call(self.client_owner, '/workspace-connections', self.body).status_code, 409)
        self.assertEqual(Workspace.objects.count(), 0)

    def test_source_failure_cannot_fall_back_to_sample(self):
        with patch.object(self.repo, 'get_source_venue_by_id', side_effect=OSError('unavailable')):
            response = self.call(self.client_owner, '/workspace-connections', self.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Workspace.objects.count(), 0)

    @override_settings(LOCAL_DEMO=False, MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=True)
    def test_direct_connection_is_not_enabled_in_non_demo_mode(self):
        self.assertEqual(self.call(self.client_owner, '/workspace-connections', self.body).status_code, 404)
        self.assertEqual(Workspace.objects.count(), 0)
