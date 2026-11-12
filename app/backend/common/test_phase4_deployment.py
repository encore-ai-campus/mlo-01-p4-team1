import json
from ipaddress import IPv4Network
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.test import SimpleTestCase, TestCase, modify_settings, override_settings

from backend.workspaces.models import BusinessEntity, Workspace, WorkspaceMembership
from backend.workspaces.tests.test_platform import Helpers, TEST_CONFIG


@modify_settings(MIDDLEWARE={'prepend': 'backend.cafe.ingress.TrustedCloudFrontScheme'})
@override_settings(
    SECURE_PROXY_SSL_HEADER=('HTTP_X_MLO_TRUSTED_SCHEME', 'https'),
    PHASE4_TRUSTED_PROXY_CIDRS=(IPv4Network('10.40.8.0/24'),),
    PHASE4_WORKER_TOKEN='x' * 32,
    PHASE4_ALLOW_INSECURE_WORKER_LOCAL=False,
)
class Phase4IngressTests(TestCase):
    def test_cloudfront_scheme_requires_a_trusted_peer_and_a_valid_token(self):
        endpoint = '/api/phase4/worker/tasks'
        token = {'HTTP_AUTHORIZATION': 'Bearer ' + 'x' * 32}
        proxy = {'REMOTE_ADDR': '10.40.8.10'}
        cloudfront = {'HTTP_CLOUDFRONT_FORWARDED_PROTO': 'https'}
        self.assertEqual(self.client.get(endpoint, **proxy, **cloudfront, **token).status_code, 200)
        self.assertEqual(self.client.get(endpoint, **proxy, **cloudfront).status_code, 401)
        denied = (
            {'REMOTE_ADDR': '198.51.100.10', **cloudfront},
            {**proxy, 'HTTP_X_MLO_TRUSTED_SCHEME': 'https'},
            {**proxy, 'HTTP_CLOUDFRONT_FORWARDED_PROTO': 'https,http'},
        )
        for headers in denied:
            with self.subTest(headers=headers):
                response = self.client.get(endpoint, **headers, **token)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['code'], 'HTTPS_REQUIRED')


class Phase4SecretFileTests(SimpleTestCase):
    def test_protected_runtime_accepts_phase4_configuration_keys(self):
        load = run_path(str(settings.BASE_DIR / 'docker/runtime/app_runtime.py'))['secret_environment']
        values = {'PHASE4_WORKER_TOKEN': 'x' * 32, 'PHASE4_OWNER_PORTAL_ENABLED': '1',
                  'PHASE4_TRUSTED_PROXY_CIDRS': '10.40.8.0/24',
                  'PHASE4_PRIVATE_DEMO_ENABLED': '1',
                  'PHASE4_PRIVATE_DEMO_POLICY_VERSION': 'privacy-handoff-v2',
                  'PHASE4_PRIVATE_DEMO_POLICY_DOCUMENTS': '{}'}
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'app-env.json'
            path.write_text(json.dumps(values), encoding='utf-8')
            path.chmod(0o600)
            self.assertEqual(load(path, root), values)
            path.write_text(json.dumps({**values, 'UNSUPPORTED_PHASE4_KEY': '1'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'unsupported secret environment keys'):
                load(path, root)


@override_settings(**{**TEST_CONFIG, 'LOCAL_DEMO': False, 'PLATFORM_ENABLED': False,
                      'MEMBER_LOCAL_PREVIEW': False, 'MEMBER_POLICIES_READY': True,
                      'PHASE4_OWNER_PORTAL_ENABLED': True})
class Phase4OwnerPortalTests(Helpers, TestCase):
    def test_approved_owner_can_access_portal_without_enabling_workspace_demo(self):
        owner, manager = self.user('portal-owner'), self.user('portal-manager')
        business = BusinessEntity.objects.create(registration_lookup='portal-fixture', registration_ciphertext='',
                                                 display_name='검증 업체')
        venue = Workspace.objects.create(business=business, location_key='portal-location', display_name='검증 매장',
                                          address_ciphertext='', status='ACTIVE')
        WorkspaceMembership.objects.create(user=owner, workspace=venue, role='OWNER')
        WorkspaceMembership.objects.create(user=manager, workspace=venue, role='MANAGER')
        owner_client = self.client_for(owner)
        manager_client = self.client_for(manager)
        endpoint = '/api/platform/v1/sessions/current'
        for client, permitted in ((owner_client, True), (manager_client, False)):
            response = client.get(endpoint, secure=True)
            result = self.data(response)
            self.response_matches(response, '/sessions/current', 'get')
            self.assertTrue(result['song_requests_only'])
            self.assertEqual(result['can_request_songs'], permitted)
        self.assertEqual(owner_client.get('/api/phase4/session', secure=True).status_code, 200)
        self.assertEqual(manager_client.get('/api/phase4/session', secure=True).status_code, 403)
        self.assertEqual(owner_client.get('/api/platform/v1/me/workspaces', secure=True).status_code, 503)
        with override_settings(PHASE4_OWNER_PORTAL_ENABLED=False):
            self.assertEqual(owner_client.get(endpoint, secure=True).status_code, 503)
