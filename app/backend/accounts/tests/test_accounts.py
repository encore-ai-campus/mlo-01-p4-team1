import base64
import hashlib
import json
import re
from datetime import timedelta
from unittest.mock import patch
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from backend.accounts import crypto, services, policy
from backend.recommendation.errors import DomainError
from backend.accounts.models import MemberProfile, AccountToken, AccountMail, AccountLimit, PolicyReceipt, SavedPlaylist

KEY = base64.b64encode(bytes(range(32))).decode()
CONFIG = dict(LOCAL_DEMO=True, MEMBER_LOCAL_PREVIEW=True, MEMBER_LOOKUP_KEY=KEY, MEMBER_EMAIL_KEYS={'test':KEY}, MEMBER_ACTIVE_EMAIL_KEY='test',
    MEMBER_POLICY_VERSION='test-v1', MEMBER_POLICY_DOCUMENTS={'terms':'Test terms', 'privacy_notice':'Test notice', 'email':'Test email notice'},
    MEMBER_MAIL_LIMIT_HOUR=100, MEMBER_RECOVERY_IP_LIMIT_HOUR=100, MEMBER_PUBLIC_ORIGIN='http://localhost:5173',
    GUEST_TTL_SECONDS=600, MEMBER_LOGIN_LIMIT_HOUR=100, MEMBER_PUBLIC_LIMIT_HOUR=100, MEMBER_SECURITY_MAIL_SECONDS=3600, ADMIN_PASSKEY_REQUIRED=False,
    EMAIL_HOST='localhost', DEFAULT_FROM_EMAIL='accounts@example.org', EMAIL_USE_TLS=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
PASSWORD = 'TestValid!@2026'
NEW_PASSWORD = 'OtherValid!@2026'


@override_settings(**CONFIG)
class AccountCase(TestCase):
    def body(self, **extra):
        return {'username':'reader', 'password':PASSWORD, 'password_confirmation':PASSWORD,
                'nickname':'테스트회원', 'terms_accepted':True, 'privacy_acknowledged':True,
                'age_confirmed':True, 'email_consent':False, 'policy_version':'test-v1', **extra}

    def post(self, path, body, client=None, method='post'):
        client = client or self.client
        csrf = client.get('/api/auth/me').json()['csrf_token']
        return getattr(client, method)(path, body, content_type='application/json', HTTP_X_CSRFTOKEN=csrf)

    def signup(self, **extra):
        response = self.post('/api/accounts/signup', self.body(**extra))
        self.assertEqual(response.status_code, 201, response.content)
        return MemberProfile.objects.get(user__username=extra.get('username','reader').lower())

    def login(self, name='reader', password=PASSWORD, client=None, **extra):
        response = self.post('/api/auth/login', {'username':name,'password':password,**extra}, client)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def code(self, user):
        message = AccountMail.objects.filter(user=user, kind='email').latest('pk')
        return re.search(r'\d{6}', crypto.unseal(message.payload)['text']).group()

    def verified(self, **extra):
        profile = self.signup(email_consent=True, email='Reader@EXAMPLE.org', **extra)
        services.verify_email(profile.user, self.code(profile.user))
        profile.refresh_from_db()
        return profile

    def recovery_link(self, user, purpose):
        text = crypto.unseal(AccountMail.objects.get(user=user, kind=purpose).payload)['text']
        kind, identifier, token = text.split('#')[1].split('/')
        return {'purpose':kind,'id':identifier,'token':token}

class AccountTests(AccountCase):
    def test_nickname_allowed_characters_length_and_no_whitespace(self):
        for value in ['가나','한글Ab12','Ab','12','회원0007','Abcde12345','ＡＢ','ㄱㄴ']:
            with self.subTest(accepted=value):
                self.assertTrue(policy.nickname(value)[0])
        for value in ['가','Abcde123456','한 글',' ab','ab ','ab\n','ab\t','漢字','😀😀','ab!']:
            with self.subTest(rejected=value), self.assertRaises(DomainError):
                policy.nickname(value)
        self.assertEqual(self.post('/api/accounts/signup',self.body(nickname='  ')).status_code,422)
        self.signup()
        self.login()
        self.assertEqual(self.post('/api/me/profile',{'nickname':'새 별명'},method='patch').status_code,422)
        self.assertEqual(self.post('/api/me/profile',{'nickname':'새별명123'},method='patch').status_code,200)

    def test_password_boundaries_composition_and_whitespace(self):
        user=get_user_model()(username='unrelatedid')
        for value in ['Abcd12!!','A'+'b'*17+'!!','ABCDE!!F','Abcd12!@']:
            with self.subTest(accepted=value):
                policy.password(value,value,user)
        for value in ['Abcd1!!','A'+'b'*18+'!!','abcd12!!','Abcd123!','Abc d1!!','Abcd12!!\n','Abcd12!!\t','Abcd한글!!','Ａbcd12!!']:
            with self.subTest(rejected=value), self.assertRaises(DomainError):
                policy.password(value,value,user)

    def test_existing_credentials_still_login_but_new_changes_require_new_policy(self):
        profile=self.verified()
        legacy='Existing long password with spaces!'
        profile.user.set_password(legacy);profile.user.save(update_fields=['password'])
        self.login(password=legacy)
        invalid='lowercase12!!'
        response=self.post('/api/me/password',{'current_password':legacy,'new_password':invalid,'password_confirmation':invalid})
        self.assertEqual(response.status_code,422)
        self.post('/api/accounts/recovery',{'purpose':'password','email':'Reader@example.org'})
        link=self.recovery_link(profile.user,'password')
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'new_password':invalid,'password_confirmation':invalid}).status_code,422)
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD}).status_code,200)
        self.login(password=NEW_PASSWORD)

    def test_without_email_signup_login_argon_and_eight_hour_session(self):
        profile = self.signup(email='discarded@example.org')
        self.assertEqual(profile.email_ciphertext, '')
        self.assertFalse(AccountMail.objects.exists())
        self.assertTrue(profile.user.password.startswith('argon2$argon2id$'))
        response = self.login('READER')
        self.assertEqual(response.json()['role'], 'USER')
        self.assertEqual(self.client.session.get_expiry_age(), 8*3600)
        self.assertEqual(self.client.get('/api/me/profile').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/feedback/metrics').status_code, 403)

    def test_remember_seven_days_and_profile_immutable_username(self):
        self.signup()
        self.login(remember=True)
        self.assertEqual(self.client.session.get_expiry_age(), 7*24*3600)
        self.assertEqual(self.post('/api/me/profile', {'username':'newname'}, method='patch').status_code, 422)

    def test_username_case_insensitive_database_guard_and_availability(self):
        self.signup()
        self.assertFalse(self.post('/api/accounts/availability', {'username':'READER'}).json()['available'])
        self.assertEqual(self.post('/api/accounts/signup', self.body(username='READER', nickname='다른별명')).status_code,409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create(username='READER')

    def test_nickname_duplicate_normalization_and_automatic_collision_retry(self):
        self.signup(nickname='ReaderNick')
        self.assertEqual(self.post('/api/accounts/signup', self.body(username='second',nickname='readernick')).status_code,409)
        with patch('backend.accounts.services.secrets.randbelow', side_effect=[7,7,8]):
            first=self.signup(username='autoone',nickname='')
            second=self.signup(username='autotwo',nickname='')
        self.assertEqual(first.nickname,'회원0007')
        self.assertEqual(second.nickname,'회원0008')

    def test_consent_age_and_policy_version_enforced_without_ip_receipts(self):
        for field in ('terms_accepted','privacy_acknowledged','age_confirmed'):
            self.assertEqual(self.post('/api/accounts/signup',self.body(**{field:False})).status_code,422)
        self.assertEqual(self.post('/api/accounts/signup',self.body(policy_version='old')).status_code,409)
        self.assertFalse(MemberProfile.objects.exists())
        self.signup()
        self.assertEqual(set(PolicyReceipt.objects.values_list('kind',flat=True)),{'terms','privacy_notice','age14'})

    def test_password_length_common_similarity_and_confirmation(self):
        for password in ('short','12345678901234567890'):
            self.assertEqual(self.post('/api/accounts/signup',self.body(password=password,password_confirmation=password)).status_code,422)
        similar='Similarusername!@'
        self.assertEqual(self.post('/api/accounts/signup',self.body(username='similarusername',password=similar,password_confirmation=similar)).status_code,422)
        self.assertEqual(self.post('/api/accounts/signup',self.body(password_confirmation='different')).status_code,422)
        self.assertEqual(self.post('/api/accounts/signup',self.body(password='x'*129,password_confirmation='x'*129)).status_code,422)

    def test_signup_requires_csrf(self):
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/api/accounts/signup',self.body(),content_type='application/json').status_code,403)
        self.assertEqual(self.post('/api/accounts/signup',self.body(),client).status_code,201)

    def test_unverified_email_never_blocks_login_or_allows_recovery(self):
        profile=self.signup(email_consent=True,email='Reader@example.org')
        self.assertIsNone(profile.email_lookup)
        self.login()
        self.assertFalse(self.client.get('/api/me/profile').json()['email_verified'])
        self.post('/api/accounts/recovery',{'purpose':'password','email':'Reader@example.org'})
        self.assertFalse(AccountMail.objects.filter(kind='password').exists())

    def test_failed_code_attempts_persist_and_fifth_attempt_locks(self):
        profile=self.signup(email_consent=True,email='Reader@example.org')
        real=self.code(profile.user)
        wrong='000000' if real!='000000' else '000001'
        self.login()
        for _ in range(5):
            self.assertEqual(self.post('/api/me/email/verify',{'code':wrong}).status_code,422)
        self.assertEqual(AccountToken.objects.get(purpose='email').attempts,5)
        self.assertEqual(self.post('/api/me/email/verify',{'code':real}).status_code,422)

    def test_email_ciphertext_hmac_and_single_account_binding(self):
        first=self.verified()
        self.assertNotIn('Reader',first.email_ciphertext)
        self.assertEqual(crypto.unseal(first.email_ciphertext),'Reader@example.org')
        self.assertEqual(first.email_lookup,crypto.digest('email','Reader@example.org'))
        AccountLimit.objects.update(last_at=timezone.now()-timedelta(seconds=61))
        other=self.signup(username='second',nickname='두번째회원',email_consent=True,email='Reader@example.org')
        with self.assertRaisesMessage(Exception,'이 이메일은 복구 주소로 사용할 수 없습니다.'):
            services.verify_email(other.user,self.code(other.user))
        # The local part is preserved; no provider-specific alias/dot collapsing.
        self.assertNotEqual(crypto.digest('email','reader@example.org'),first.email_lookup)

    def test_resend_interval_and_old_code_invalidation(self):
        profile=self.signup(email_consent=True,email='Reader@example.org')
        first=AccountToken.objects.get(user=profile.user)
        self.login()
        body={'email':'Reader@example.org','email_consent':True,'policy_version':'test-v1','current_password':PASSWORD}
        self.assertEqual(self.post('/api/me/email',body).status_code,429)
        AccountLimit.objects.update(last_at=timezone.now()-timedelta(seconds=61))
        self.assertEqual(self.post('/api/me/email',body).status_code,200)
        first.refresh_from_db()
        self.assertIsNotNone(first.consumed_at)
        self.assertFalse(AccountMail.objects.filter(token=first).exists())

    def test_recovery_generic_response_and_purpose_binding_single_use(self):
        profile=self.verified()
        known=self.post('/api/accounts/recovery',{'purpose':'username','email':'Reader@example.org'})
        missing=self.post('/api/accounts/recovery',{'purpose':'username','email':'nobody@example.org'})
        self.assertEqual((known.status_code,known.json()),(missing.status_code,missing.json()))
        link=self.recovery_link(profile.user,'username')
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'purpose':'password','new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD}).status_code,422)
        response=self.post('/api/accounts/recovery/complete',link)
        self.assertEqual(response.json(),{'username':'reader'})
        self.assertFalse(self.client.get('/api/auth/me').json()['authenticated'])
        self.assertEqual(self.post('/api/accounts/recovery/complete',link).status_code,422)

    def test_reset_revokes_all_sessions_tokens_and_requires_normal_login(self):
        profile=self.verified()
        other=Client()
        self.login(client=other)
        self.login()
        self.post('/api/accounts/recovery',{'purpose':'password','email':'Reader@example.org'})
        link=self.recovery_link(profile.user,'password')
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD}).status_code,200)
        for client in (self.client,other):
            self.assertFalse(client.get('/api/auth/me').json()['authenticated'])
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD}).status_code,422)
        self.login(password=NEW_PASSWORD)

    def test_email_change_requires_password_keeps_old_until_confirmed_and_revokes_tokens(self):
        profile=self.verified()
        old_lookup=profile.email_lookup
        self.login()
        self.post('/api/accounts/recovery',{'purpose':'password','email':'Reader@example.org'})
        link=self.recovery_link(profile.user,'password')
        body={'email':'new@example.org','email_consent':True,'policy_version':'test-v1'}
        self.assertEqual(self.post('/api/me/email',body).status_code,403)
        AccountLimit.objects.update(last_at=timezone.now()-timedelta(seconds=61))
        self.assertEqual(self.post('/api/me/email',{**body,'current_password':PASSWORD}).status_code,200)
        profile.refresh_from_db()
        self.assertEqual(profile.email_lookup,old_lookup)
        self.assertEqual(self.post('/api/me/email/verify',{'code':self.code(profile.user)}).status_code,200)
        self.assertTrue(AccountMail.objects.filter(kind='security').exists())
        self.assertEqual(self.post('/api/accounts/recovery/complete',{**link,'new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD}).status_code,422)

    def test_email_withdrawal_deletes_queue_pending_address_and_recovery_authority(self):
        profile=self.verified()
        self.post('/api/accounts/recovery',{'purpose':'password','email':'Reader@example.org'})
        self.login()
        self.assertEqual(self.post('/api/me/email',{'current_password':PASSWORD},method='delete').status_code,200)
        profile.refresh_from_db()
        self.assertIsNone(profile.email_lookup)
        self.assertEqual(profile.email_ciphertext,'')
        self.assertFalse(AccountMail.objects.exists())
        self.assertFalse(AccountToken.objects.filter(consumed_at=None).exists())

    def test_password_change_keeps_current_and_invalidates_other_session(self):
        self.signup()
        other=Client()
        self.login(client=other)
        self.login()
        response=self.post('/api/me/password',{'current_password':PASSWORD,'new_password':NEW_PASSWORD,'password_confirmation':NEW_PASSWORD})
        self.assertEqual(response.status_code,200)
        self.assertTrue(self.client.get('/api/auth/me').json()['authenticated'])
        self.assertFalse(other.get('/api/auth/me').json()['authenticated'])

    def test_withdrawal_deletes_member_queue_receipts_and_sessions(self):
        profile=self.verified()
        self.login()
        SavedPlaylist.objects.create(user=profile.user,source_id='fixture',snapshot={'items':[]})
        response=self.post('/api/me/withdraw',{'current_password':PASSWORD,'confirmation':'회원탈퇴'})
        self.assertEqual(response.status_code,200,response.content)
        self.assertFalse(MemberProfile.objects.exists())
        self.assertFalse(get_user_model().objects.filter(pk=profile.user_id).exists())
        self.assertFalse(AccountMail.objects.exists())
        self.assertFalse(PolicyReceipt.objects.exists())
        self.assertFalse(SavedPlaylist.objects.exists())
        self.assertFalse(self.client.get('/api/auth/me').json()['authenticated'])

    def test_saved_history_is_owned_and_idempotent(self):
        from backend.recommendation.models import Catalog,Family,Job,Playlist
        from backend.common.identity import owner
        profile=self.signup()
        self.login()
        catalog=Catalog.objects.create(version='member-test',tracks=[],sha256='test',source_ref='test',source_sha256='test')
        family=Family.objects.create(owner_id=str(profile.owner_id),options={},policy_version='test',mapping_version='test')
        job=Job.objects.create(family=family,catalog=catalog,input={},request_id='test')
        playlist=Playlist.objects.create(job=job,family=family,snapshot={'summary':'테스트','items':[]})
        row=self.post('/api/me/playlists',{'playlist_id':playlist.pk})
        self.assertEqual(row.status_code,201)
        identifier=row.json()['id']
        self.assertEqual(self.post('/api/me/playlists',{'playlist_id':playlist.pk}).status_code,200)
        self.post('/api/auth/logout',{})
        self.signup(username='second',nickname='두번째회원')
        self.login('second')
        self.assertEqual(self.client.get(f'/api/me/playlists/{identifier}').status_code,404)
        self.assertEqual(self.post(f'/api/me/playlists/{identifier}',{},method='delete').status_code,404)
        self.assertEqual(self.post('/api/me/playlists',{'playlist_id':playlist.pk}).status_code,404)

    def test_mail_delivery_failure_is_retryable_without_plaintext_error_and_revoked_jobs_do_not_send(self):
        from backend.accounts.mail import deliver_one
        profile=self.signup(email_consent=True,email='Reader@example.org')
        with patch('backend.accounts.mail.EmailMessage.send',side_effect=RuntimeError('private@example.org secret-token')):
            self.assertTrue(deliver_one())
        row=AccountMail.objects.get()
        self.assertEqual(row.last_error,'RuntimeError')
        self.assertEqual(row.state,'PENDING')
        row.next_attempt_at=timezone.now();row.save()
        self.assertTrue(deliver_one())
        from django.core import mail
        self.assertEqual(len(mail.outbox),1)
        self.assertEqual(mail.outbox[0].to,['Reader@example.org'])
        self.assertFalse(AccountMail.objects.exists())

    def test_ciphertext_tamper_and_key_rotation(self):
        sealed=crypto.seal('Reader@example.org')
        self.assertEqual(crypto.unseal(sealed),'Reader@example.org')
        with self.assertRaises(Exception):
            crypto.unseal(sealed[:-4]+'AAAA')
        with override_settings(MEMBER_ACTIVE_EMAIL_KEY='next',MEMBER_EMAIL_KEYS={'test':KEY,'next':base64.b64encode(b'n'*32).decode()}):
            self.assertEqual(crypto.unseal(sealed),'Reader@example.org')
            self.assertTrue(crypto.seal('new@example.org').startswith('next.'))

    def test_production_signup_disabled_without_final_policy(self):
        with override_settings(LOCAL_DEMO=False,MEMBER_LOCAL_PREVIEW=False,MEMBER_POLICIES_READY=False):
            self.assertEqual(self.post('/api/accounts/signup',self.body()).status_code,503)

    def test_malformed_json_container_and_public_request_limit(self):
        self.assertEqual(self.post('/api/accounts/signup',[]).status_code,422)
        with override_settings(MEMBER_PUBLIC_LIMIT_HOUR=1):
            self.assertEqual(self.post('/api/accounts/availability',{'username':'reader'}).status_code,200)
            self.assertEqual(self.post('/api/accounts/availability',{'username':'reader'}).status_code,429)

    def test_address_rate_limit_does_not_block_basic_signup(self):
        self.signup(email_consent=True,email='shared@example.org')
        response=self.post('/api/accounts/signup',self.body(username='second',nickname='다른회원',email_consent=True,email='shared@example.org'))
        self.assertEqual(response.status_code,201,response.content)
        self.assertTrue(response.json()['email_deferred'])
        self.assertFalse(response.json()['email_pending'])
        self.login('second')

    def test_expired_tokens_and_expired_mail_payload_are_not_usable(self):
        from django.core.management import call_command
        profile=self.signup(email_consent=True,email='expiry@example.org')
        code=self.code(profile.user)
        AccountToken.objects.filter(user=profile.user).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.login()
        self.assertEqual(self.post('/api/me/email/verify',{'code':code}).status_code,422)
        call_command('cleanup_account_tokens')
        self.assertFalse(AccountMail.objects.exists())
        self.assertFalse(AccountToken.objects.exists())
