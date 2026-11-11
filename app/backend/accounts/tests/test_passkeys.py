import hashlib
import json
import secrets
import struct
import cbor2
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.utils import timezone
from datetime import timedelta
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from backend.accounts.crypto import digest
from backend.accounts.models import AccountLimit, PasskeyEnrollment, AdminPasskey
from .test_accounts import AccountCase, CONFIG, PASSWORD


@override_settings(**{**CONFIG,'ADMIN_PASSKEY_REQUIRED':True,'ADMIN_PASSKEY_RP_ID':'localhost',
    'ADMIN_PASSKEY_ORIGIN':'http://localhost:5173','ADMIN_SESSION_SECONDS':600,'ADMIN_LOCAL_LOGIN_ENABLED':True})
class PasskeyTests(AccountCase):

    def setUp(self):
        self.admin=get_user_model().objects.create_user(username='test-admin',password=PASSWORD,is_staff=True)
        self.admin.user_permissions.add(Permission.objects.get(content_type__app_label='common',codename='manage_music'))
        self.ticket=secrets.token_urlsafe(32)
        PasskeyEnrollment.objects.create(user=self.admin,digest=hashlib.sha256(self.ticket.encode()).hexdigest(),expires_at=timezone.now()+timedelta(minutes=10))
        self.key=ec.generate_private_key(ec.SECP256R1())
        self.credential_id=secrets.token_bytes(32)

    def primary(self):
        response=self.login('test-admin')
        self.assertTrue(response.json()['passkey_required'])
        self.assertEqual(response.json()['landing_path'],'/admin/passkey')

    def registration(self,challenge,origin='http://localhost:5173',flags=0x45):
        public=self.key.public_key().public_numbers()
        cose={1:2,3:-7,-1:1,-2:public.x.to_bytes(32,'big'),-3:public.y.to_bytes(32,'big')}
        data=hashlib.sha256(b'localhost').digest()+bytes([flags])+struct.pack('>I',0)+bytes(16)+struct.pack('>H',len(self.credential_id))+self.credential_id+cbor2.dumps(cose)
        client=json.dumps({'type':'webauthn.create','challenge':challenge,'origin':origin}).encode()
        return {'id':bytes_to_base64url(self.credential_id),'rawId':bytes_to_base64url(self.credential_id),'type':'public-key',
                'response':{'clientDataJSON':bytes_to_base64url(client),'attestationObject':bytes_to_base64url(cbor2.dumps({'fmt':'none','attStmt':{},'authData':data}))}}

    def register(self):
        self.primary()
        options=self.post('/api/admin/passkeys/options',{'mode':'register','enrollment_ticket':self.ticket})
        self.assertEqual(options.status_code,200,options.content)
        result=self.post('/api/admin/passkeys/verify',{'enrollment_ticket':self.ticket,'credential':self.registration(options.json()['challenge'])})
        self.assertEqual(result.status_code,200,result.content)
        return result

    def test_admin_pending_cannot_access_management_and_requires_operator_enrollment(self):
        self.primary()
        self.assertEqual(self.client.get('/api/admin/feedback/metrics').status_code,403)
        self.assertFalse(self.client.get('/api/auth/me').json()['authenticated'])
        self.assertEqual(self.post('/api/admin/passkeys/options',{'mode':'register','enrollment_ticket':'wrong'}).status_code,403)

    def test_real_webauthn_registration_signature_authentication_and_replay(self):
        attempt_key = digest('rate', 'login:test-admin')
        self.assertEqual(self.register().json()['role'],'ADMIN')
        self.assertEqual(AccountLimit.objects.get(pk=attempt_key).count, 0)
        self.assertFalse(PasskeyEnrollment.objects.exists())
        self.post('/api/auth/logout',{})
        self.primary()
        self.assertEqual(AccountLimit.objects.get(pk=attempt_key).count, 1)
        options=self.post('/api/admin/passkeys/options',{'mode':'authenticate'}).json()
        client=json.dumps({'type':'webauthn.get','challenge':options['challenge'],'origin':'http://localhost:5173'}).encode()
        data=hashlib.sha256(b'localhost').digest()+bytes([0x05])+struct.pack('>I',1)
        signature=self.key.sign(data+hashlib.sha256(client).digest(),ec.ECDSA(hashes.SHA256()))
        credential={'id':bytes_to_base64url(self.credential_id),'rawId':bytes_to_base64url(self.credential_id),'type':'public-key',
            'response':{'clientDataJSON':bytes_to_base64url(client),'authenticatorData':bytes_to_base64url(data),'signature':bytes_to_base64url(signature),'userHandle':None}}
        response=self.post('/api/admin/passkeys/verify',{'credential':credential})
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(AdminPasskey.objects.get().sign_count,1)
        self.assertEqual(AccountLimit.objects.get(pk=attempt_key).count, 0)
        self.assertEqual(self.post('/api/admin/passkeys/verify',{'credential':credential}).status_code,422)

    def test_wrong_origin_and_missing_user_verification_are_rejected(self):
        self.primary()
        for origin,flags in [('https://attacker.example.org',0x45),('http://localhost:5173',0x41)]:
            options=self.post('/api/admin/passkeys/options',{'mode':'register','enrollment_ticket':self.ticket}).json()
            response=self.post('/api/admin/passkeys/verify',{'credential':self.registration(options['challenge'],origin,flags),'enrollment_ticket':self.ticket})
            self.assertEqual(response.status_code,403,response.content)
            self.assertFalse(AdminPasskey.objects.exists())
        self.assertEqual(AccountLimit.objects.get(pk=digest('rate', 'login:test-admin')).count, 1)

    def test_member_cannot_enroll_admin_passkeys(self):
        self.signup()
        self.login()
        self.assertEqual(self.post('/api/admin/passkeys/options',{'mode':'register','enrollment_ticket':self.ticket}).status_code,401)
