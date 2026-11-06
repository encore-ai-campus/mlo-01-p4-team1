from unittest.mock import patch
from django.test import SimpleTestCase, override_settings


@override_settings(APP_REVISION='b'*64)
class ReadinessTests(SimpleTestCase):
    def test_revision_is_returned_only_when_dependencies_and_schema_are_ready(self):
        with patch('api.readiness.databases_ready', return_value=True):
            response = self.client.get('/readyz')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'status':'ready','revision':'b'*64})

    def test_missing_revision_or_dependency_failure_is_not_ready_and_is_sanitized(self):
        with override_settings(APP_REVISION=''):
            self.assertEqual(self.client.get('/readyz').status_code, 503)
        for kwargs in ({'return_value':False}, {'side_effect':RuntimeError('private-uri-password')}):
            with patch('api.readiness.databases_ready', **kwargs):
                response = self.client.get('/readyz')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'status':'unavailable'})
