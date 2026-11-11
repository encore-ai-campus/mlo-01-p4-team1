from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings

from backend.accounts.models import MemberProfile
from backend.workspaces.models import ServiceMembership


@override_settings(PLATFORM_ENABLED=True, MEMBER_LOCAL_PREVIEW=True, ALLOWED_HOSTS=['testserver'])
class VisitEndpointAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name, kind in (('visit_guest', 'CONSUMER'), ('visit_owner', 'BUSINESS')):
            user = get_user_model().objects.create_user(username=name)
            MemberProfile.objects.create(user=user, nickname=name, nickname_key=name)
            ServiceMembership.objects.create(user=user, kind=kind, status='ACTIVE')
            setattr(cls, name, user)

    def test_consumer_can_call_structured_search_only_with_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.visit_guest)
        options = client.get('/api/venues/ai-concierge/options')
        self.assertEqual(options.status_code, 200)
        token = client.cookies['csrftoken'].value
        path = '/api/venues/concierge/visit'
        body = {'mcp': {'region': '인천 연수구'}}
        self.assertEqual(client.post(path, body, content_type='application/json').status_code, 403)
        with patch('backend.venues.views.concierge_view._mcp_recommendation',
                   return_value=JsonResponse({'success': True, 'mcp': {}})) as recommend:
            response = client.post(path, body, content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(recommend.call_args.args[1], body['mcp'])
        self.assertEqual(client.post(path, {'query': '카페'}, content_type='application/json',
                                     HTTP_X_CSRFTOKEN=token).status_code, 400)
        self.assertEqual(client.post(path, 'x', content_type='text/plain',
                                     HTTP_X_CSRFTOKEN=token).status_code, 415)

    def test_business_and_anonymous_accounts_cannot_use_platform_search(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.visit_guest)
        client.get('/api/venues/ai-concierge/options')
        token = client.cookies['csrftoken'].value
        client.force_login(self.visit_owner)
        path = '/api/venues/concierge/visit'
        self.assertEqual(client.post(path, {'mcp': {}}, content_type='application/json',
                                     HTTP_X_CSRFTOKEN=token).status_code, 403)
        client.logout()
        # Logout rotates/removes the CSRF cookie, so middleware rejects the
        # anonymous write before the endpoint can inspect its session.
        self.assertEqual(client.post(path, {'mcp': {}}, content_type='application/json',
                                     HTTP_X_CSRFTOKEN=token).status_code, 403)
