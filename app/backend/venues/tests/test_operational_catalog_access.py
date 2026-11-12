import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import path

from backend.accounts.models import MemberProfile
from backend.venues.views.access import consumer_get, consumer_post
from backend.workspaces.models import ServiceMembership


@consumer_get(operational_catalog=True)
def catalog_view(request, *, consumer):
    return JsonResponse({'consumer_id': consumer.pk})


@consumer_get
def ordinary_consumer_view(request, *, consumer):
    return JsonResponse({'consumer_id': consumer.pk})


@consumer_post
def ordinary_consumer_post(request, *, consumer):
    return JsonResponse({'consumer_id': consumer.pk})


urlpatterns = [
    path('catalog', catalog_view),
    path('ordinary', ordinary_consumer_view),
    path('ordinary-write', ordinary_consumer_post),
]


@override_settings(
    ROOT_URLCONF=__name__,
    LOCAL_DEMO=False,
    MEMBER_LOCAL_PREVIEW=False,
    PLATFORM_ENABLED=False,
    ALLOWED_HOSTS=['testserver'],
)
class OperationalCatalogAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.consumer = get_user_model().objects.create_user(username='catalog-consumer', password='test-password')
        MemberProfile.objects.create(user=cls.consumer, nickname='consumer', nickname_key='catalog-consumer')
        ServiceMembership.objects.create(user=cls.consumer, kind='CONSUMER', status='ACTIVE')
        cls.business = get_user_model().objects.create_user(username='catalog-business', password='test-password')
        MemberProfile.objects.create(user=cls.business, nickname='business', nickname_key='catalog-business')
        ServiceMembership.objects.create(user=cls.business, kind='BUSINESS', status='ACTIVE')

    def test_requires_an_explicit_operational_catalog(self):
        self.client.force_login(self.consumer)
        for value in ('', '/data/catalog.json', '/data/catalog.sqlite3?wrong'):
            with self.subTest(value=value), patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': value}):
                response = self.client.get('/catalog')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['code'], 'PLATFORM_NOT_READY')

    @patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': '/data/catalog.sqlite3'})
    def test_checks_session_account_and_active_consumer_role(self):
        anonymous = self.client.get('/catalog')
        self.assertEqual(anonymous.status_code, 401)
        self.assertIn('no-store', anonymous.headers['Cache-Control'])

        self.client.force_login(self.business)
        self.assertEqual(self.client.get('/catalog').status_code, 403)

        self.client.force_login(self.consumer)
        allowed = self.client.get('/catalog')
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()['consumer_id'], self.consumer.pk)
        self.assertIn('no-store', allowed.headers['Cache-Control'])

        ServiceMembership.objects.filter(user=self.consumer, kind='CONSUMER').update(status='SUSPENDED')
        self.assertEqual(self.client.get('/catalog').status_code, 403)

    @patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': '/data/catalog.sqlite'})
    def test_inactive_and_stale_sessions_are_rejected(self):
        self.client.force_login(self.consumer)
        self.consumer.is_active = False
        self.consumer.save(update_fields=['is_active'])
        self.assertEqual(self.client.get('/catalog').status_code, 401)

        self.consumer.is_active = True
        self.consumer.save(update_fields=['is_active'])
        self.client.force_login(self.consumer)
        self.consumer.set_password('new-password')
        self.consumer.save(update_fields=['password'])
        self.assertEqual(self.client.get('/catalog').status_code, 401)

    @patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': '/data/catalog.sqlite3'})
    def test_missing_profile_and_staff_account_are_rejected(self):
        user = get_user_model().objects.create_user(username='missing-profile')
        ServiceMembership.objects.create(user=user, kind='CONSUMER', status='ACTIVE')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/catalog').status_code, 401)

        staff = get_user_model().objects.create_user(username='staff-consumer', is_staff=True)
        MemberProfile.objects.create(user=staff, nickname='staff', nickname_key='staff-consumer')
        ServiceMembership.objects.create(user=staff, kind='CONSUMER', status='ACTIVE')
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/catalog').status_code, 403)

    @patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': '/data/catalog.sqlite3'})
    def test_other_read_and_write_endpoints_keep_their_existing_guard(self):
        self.client.force_login(self.consumer)
        self.assertEqual(self.client.get('/ordinary').status_code, 503)
        self.assertEqual(self.client.post('/ordinary-write').status_code, 503)

    @override_settings(LOCAL_DEMO=True, MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=True)
    @patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': '/data/catalog.sqlite3'})
    def test_local_platform_mode_still_requires_a_consumer(self):
        self.assertEqual(self.client.get('/catalog').status_code, 401)
        self.client.force_login(self.consumer)
        self.assertEqual(self.client.get('/catalog').status_code, 200)

    @override_settings(LOCAL_DEMO=True, MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=False)
    def test_standalone_demo_remains_available(self):
        @consumer_get(operational_catalog=True)
        def demo_view(request, *, consumer):
            return JsonResponse({'standalone': consumer is None})

        from django.test import RequestFactory

        response = demo_view(RequestFactory().get('/'))
        self.assertEqual(response.status_code, 200)
        self.assertJSONEqual(response.content, {'standalone': True})
