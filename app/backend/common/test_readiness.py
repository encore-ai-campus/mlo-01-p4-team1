from unittest.mock import patch
from django.test import TestCase, override_settings


@override_settings(APP_REVISION='a'*64)
class ReadinessTests(TestCase):
    def test_applied_database_returns_the_immutable_revision(self):
        response = self.client.get('/readyz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status':'ready', 'revision':'a'*64})

    def test_missing_revision_pending_schema_or_database_failure_is_unavailable(self):
        with override_settings(APP_REVISION='mutable-tag'):
            self.assertEqual(self.client.get('/readyz').status_code, 503)
        for value in (False, RuntimeError('private-database-password')):
            kwargs = {'side_effect':value} if isinstance(value, Exception) else {'return_value':value}
            with patch('backend.common.readiness.database_ready', **kwargs):
                response = self.client.get('/readyz')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {'status':'unavailable'})
