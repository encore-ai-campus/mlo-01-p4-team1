"""Explicit PostgreSQL concurrency gate; never substitute SQLite or skip it."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, close_old_connections
from django.test import TransactionTestCase, override_settings
from backend.accounts import services, crypto
from backend.accounts.models import MemberProfile, AccountMail, AccountToken, AccountLimit
from django.utils import timezone
from datetime import timedelta
from .test_accounts import CONFIG, PASSWORD


@override_settings(**{**CONFIG, 'LOCAL_DEMO':False})
class PostgresConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor,'postgresql','this gate requires a disposable PostgreSQL database')

    def body(self,name,nick):
        return dict(username=name,nickname=nick,password=PASSWORD,password_confirmation=PASSWORD,
            terms_accepted=True,privacy_acknowledged=True,age_confirmed=True,email_consent=False,policy_version='test-v1')

    def parallel(self, functions):
        barrier=Barrier(len(functions))
        def run(fn):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                fn()
                return 'ok'
            except Exception as error:
                return getattr(error,'code',type(error).__name__)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run,functions))

    def test_case_insensitive_signup_race(self):
        result=self.parallel([lambda:services.signup(self.body('SameUser','첫번째회원')),
                              lambda:services.signup(self.body('sameuser','두번째회원'))])
        self.assertCountEqual(result,['ok','USERNAME_TAKEN'])
        self.assertEqual(MemberProfile.objects.count(),1)

    def test_nickname_race(self):
        result=self.parallel([lambda:services.signup(self.body('first','같은별명')),
                              lambda:services.signup(self.body('second','같은별명'))])
        self.assertCountEqual(result,['ok','NICKNAME_TAKEN'])
        self.assertEqual(MemberProfile.objects.count(),1)

    def test_verified_email_can_bind_to_only_one_account(self):
        import re
        first=services.signup({**self.body('first','첫번째회원'),'email_consent':True,'email':'shared@example.org'})
        AccountLimit.objects.update(last_at=timezone.now()-timedelta(seconds=61))
        second=services.signup({**self.body('second','두번째회원'),'email_consent':True,'email':'shared@example.org'})
        def code(user):
            return re.search(r'\d{6}',crypto.unseal(AccountMail.objects.get(user=user).payload)['text']).group()
        a,b=code(first.user),code(second.user)
        result=self.parallel([lambda:services.verify_email(first.user,a),lambda:services.verify_email(second.user,b)])
        self.assertCountEqual(result,['ok','EMAIL_UNAVAILABLE'])
        self.assertEqual(MemberProfile.objects.filter(email_lookup__isnull=False).count(),1)

    def test_token_is_consumed_once_across_connections(self):
        import re
        profile=services.signup({**self.body('reader','테스트회원'),'email_consent':True,'email':'reader@example.org'})
        code=re.search(r'\d{6}',crypto.unseal(AccountMail.objects.get(user=profile.user).payload)['text']).group()
        services.verify_email(profile.user,code)
        services.recovery_request('username','reader@example.org','127.0.0.1')
        payload=crypto.unseal(AccountMail.objects.get(kind='username').payload)
        _,identifier,raw=payload['text'].split('#')[1].split('/')
        result=self.parallel([lambda:services.consume_recovery('username',identifier,raw),
                              lambda:services.consume_recovery('username',identifier,raw)])
        self.assertCountEqual(result,['ok','INVALID_TOKEN'])

    def test_rate_limit_shared_across_connections(self):
        result=self.parallel([lambda:self.assertTrue(services.limit('shared-limit',interval=60,maximum=1)),
                              lambda:self.assertTrue(services.limit('shared-limit',interval=60,maximum=1))])
        self.assertCountEqual(result,['ok','AssertionError'])
