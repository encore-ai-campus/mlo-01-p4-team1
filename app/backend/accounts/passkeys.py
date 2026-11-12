"""WebAuthn step-up for existing administrator primary authentication."""
import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.contrib.auth import get_user
from django.db import transaction, IntegrityError
from django.utils import timezone
from rest_framework.response import Response
from webauthn import (generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response, options_to_json)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, UserVerificationRequirement,
    ResidentKeyRequirement, PublicKeyCredentialDescriptor)
from .endpoint import Endpoint
from backend.recommendation.errors import DomainError, require
from backend.common.identity import raw_request
from .models import AdminPasskey, PasskeyChallenge, PasskeyEnrollment


def complete(request):
    session = raw_request(request).session
    return not settings.ADMIN_PASSKEY_REQUIRED or session.get('admin_passkey_user') == session.get('_auth_user_id')


def begin_primary(request):
    session = raw_request(request).session
    session.pop('admin_passkey_user', None)
    session.pop('admin_passkey_verified_at', None)
    session['admin_primary_at'] = time.time()


def primary_user(request):
    user = get_user(raw_request(request))
    require(user.is_authenticated and user.is_active and user.is_staff,
            'AUTH_REQUIRED', '관리자 기본 인증이 필요합니다.', 401)
    require(time.time()-raw_request(request).session.get('admin_primary_at', 0) <= 300,
            'REAUTH_REQUIRED', '관리자 로그인을 다시 진행해 주세요.', 401)
    return user


def configuration():
    rp, origin = settings.ADMIN_PASSKEY_RP_ID, settings.ADMIN_PASSKEY_ORIGIN
    parsed = urlsplit(origin)
    require(rp and parsed.hostname == rp and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            and not parsed.username and (parsed.scheme == 'https' or
                (settings.MEMBER_LOCAL_PREVIEW and rp == 'localhost' and parsed.scheme == 'http')),
            'PASSKEY_NOT_READY', '패스키 도메인 설정을 확인해 주세요.', 503)
    return rp, origin.rstrip('/')


def session_digest(request):
    session = raw_request(request).session
    if not session.session_key:
        session.create()
    return hashlib.sha256(session.session_key.encode()).hexdigest()


def enrollment_allowed(user, session, ticket):
    if AdminPasskey.objects.filter(user=user).exists():
        require(session.get('admin_passkey_user') == str(user.pk),
                'PASSKEY_REQUIRED', '기존 패스키로 먼저 인증해 주세요.', 403)
    else:
        row = PasskeyEnrollment.objects.select_for_update().filter(user=user, expires_at__gt=timezone.now()).first()
        supplied = hashlib.sha256(ticket.encode()).hexdigest() if isinstance(ticket, str) and len(ticket) <= 128 else ''
        require(row and hmac.compare_digest(row.digest, supplied),
                'ENROLLMENT_REQUIRED', '운영자가 발급한 유효한 패스키 등록권이 필요합니다.', 403)


class Options(Endpoint):
    def post(self, request):
        user = primary_user(request)
        mode = request.data.get('mode')
        require(mode in ('register', 'authenticate'))
        rp, _ = configuration()
        with transaction.atomic():
            # Serialize first enrollment and subsequent key changes for each administrator.
            from django.contrib.auth import get_user_model
            get_user_model().objects.select_for_update().get(pk=user.pk)
            keys = list(AdminPasskey.objects.filter(user=user))
            descriptors = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.credential_id)) for k in keys]
            if mode == 'register':
                enrollment_allowed(user, raw_request(request).session, request.data.get('enrollment_ticket'))
                options = generate_registration_options(rp_id=rp, rp_name='muther 관리자',
                    user_id=hashlib.sha256(('admin:'+str(user.pk)).encode()).digest(), user_name=user.username,
                    exclude_credentials=descriptors,
                    authenticator_selection=AuthenticatorSelectionCriteria(
                        user_verification=UserVerificationRequirement.REQUIRED,
                        resident_key=ResidentKeyRequirement.PREFERRED))
            else:
                require(keys, 'NO_PASSKEY', '등록된 패스키가 없습니다. 운영자에게 등록권을 요청해 주세요.', 409)
                options = generate_authentication_options(rp_id=rp, allow_credentials=descriptors,
                    user_verification=UserVerificationRequirement.REQUIRED)
            PasskeyChallenge.objects.filter(user=user, session_digest=session_digest(request)).delete()
            row = PasskeyChallenge.objects.create(user=user, purpose=mode, challenge=options.challenge,
                session_digest=session_digest(request), expires_at=timezone.now()+timedelta(minutes=5))
        raw_request(request).session['passkey_challenge'] = str(row.pk)
        return Response(json.loads(options_to_json(options)))


class Verify(Endpoint):
    def post(self, request):
        from rest_framework.response import Response
        from django.contrib.auth import get_user_model
        user = primary_user(request)
        rp, origin = configuration()
        session = raw_request(request).session
        identifier = session.pop('passkey_challenge', None)
        require(identifier, 'INVALID_CHALLENGE', '패스키 인증을 다시 시작해 주세요.')
        error = False
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            row = PasskeyChallenge.objects.select_for_update().filter(pk=identifier, user=user,
                session_digest=session_digest(request), expires_at__gt=timezone.now()).first()
            require(row, 'INVALID_CHALLENGE', '패스키 인증이 만료되었습니다.')
            credential = request.data.get('credential')
            require(isinstance(credential, dict) and len(json.dumps(credential)) < 32768)
            try:
                if row.purpose == 'register':
                    enrollment_allowed(user, session, request.data.get('enrollment_ticket'))
                    result = verify_registration_response(credential=credential, expected_challenge=bytes(row.challenge),
                        expected_rp_id=rp, expected_origin=origin, require_user_verification=True)
                    with transaction.atomic():
                        AdminPasskey.objects.create(user=user, credential_id=bytes_to_base64url(result.credential_id),
                            public_key=result.credential_public_key, sign_count=result.sign_count)
                    PasskeyEnrollment.objects.filter(user=user).delete()
                else:
                    key = AdminPasskey.objects.select_for_update().get(user=user, credential_id=credential.get('id'))
                    result = verify_authentication_response(credential=credential, expected_challenge=bytes(row.challenge),
                        expected_rp_id=rp, expected_origin=origin, credential_public_key=bytes(key.public_key),
                        credential_current_sign_count=key.sign_count, require_user_verification=True)
                    key.sign_count = result.new_sign_count
                    key.save(update_fields=['sign_count'])
            except Exception:
                error = True
            row.delete()  # Also consume a failed ceremony; failures cannot roll back the challenge deletion.
        require(not error, 'PASSKEY_FAILED', '패스키를 확인할 수 없습니다. 다시 시도해 주세요.', 403)
        from backend.common.api import account_data, account_data_for_user
        menus = account_data_for_user(user)
        require(menus, 'FORBIDDEN', '사용 가능한 관리자 메뉴가 없습니다.', 403)
        if settings.ADMIN_LOCAL_LOGIN_ENABLED and settings.MEMBER_LOOKUP_KEY:
            # Only a complete administrator sign-in resets the password attempt counter.
            from .crypto import digest
            from .models import AccountLimit
            identifier = digest('rate', 'login:' + user.username.lower())
            AccountLimit.objects.filter(pk=identifier).update(count=0)
        session.cycle_key()
        session['admin_passkey_user'] = str(user.pk)
        session['admin_passkey_verified_at'] = timezone.now().timestamp()
        from backend.workspaces import private_demo

        account = account_data(request)
        if private_demo.review_only_mode() and account['private_demo_review_allowed']:
            return Response({**account, 'landing_path': '/platform/admin'})
        return Response({**account, 'landing_path': menus[0]['path']})
