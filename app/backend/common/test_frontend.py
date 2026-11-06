from pathlib import Path
from tempfile import TemporaryDirectory
from django.test import TestCase, override_settings


class FrontendServingTests(TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root/'assets').mkdir()
        (self.root/'index.html').write_text('<!doctype html><div id="root"></div>')
        (self.root/'assets/index-hashed.js').write_text('console.log("public");')
        self.override = override_settings(FRONTEND_DIST=self.root, FRONTEND_REQUIRED=True, APP_REVISION='a'*64)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_direct_navigation_serves_spa_without_api_fallback(self):
        for path in ('/','/signup','/mypage','/account/recovery','/admin/passkey'):
            response = self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'id="root"',b''.join(response.streaming_content))
            self.assertEqual(response['Cache-Control'],'no-store')
        self.assertEqual(self.client.get('/api/no-such-route').status_code,404)
        self.assertEqual(self.client.post('/signup',{}).status_code,405)

    def test_only_public_build_assets_are_served_and_no_traversal(self):
        response=self.client.get('/assets/index-hashed.js')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Cache-Control'],'public, max-age=31536000, immutable')
        self.assertEqual(response['X-Content-Type-Options'],'nosniff')
        response.close()
        (self.root/'assets/private.env').write_text('not-public')
        for path in ('/assets/private.env','/assets/../../backend/cafe/settings.py','/backend/cafe/settings.py','/src/main.tsx'):
            self.assertEqual(self.client.get(path).status_code,404)

    def test_missing_frontend_build_fails_production_readiness(self):
        self.assertEqual(self.client.get('/readyz').status_code,200)
        (self.root/'index.html').unlink()
        self.assertEqual(self.client.get('/signup').status_code,503)
        self.assertEqual(self.client.get('/readyz').status_code,503)
