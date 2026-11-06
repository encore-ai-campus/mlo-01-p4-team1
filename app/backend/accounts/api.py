from django.contrib.auth import get_user, get_user_model, login, logout, update_session_auth_hash
from django.db import transaction, IntegrityError
from django.utils import timezone
from rest_framework.response import Response
from .endpoint import Endpoint
from backend.recommendation.errors import require, DomainError
from backend.common.identity import raw_request
from . import services, policy
from .models import MemberProfile, AccountToken, SavedPlaylist, AccountMail


def member(request):
    user = get_user(raw_request(request))
    require(user.is_authenticated and user.is_active and not user.is_staff,
            'AUTH_REQUIRED', '회원 로그인이 필요합니다.', 401)
    profile = MemberProfile.objects.filter(user=user).first()
    require(profile, 'AUTH_REQUIRED', '회원 로그인이 필요합니다.', 401)
    return profile


def current_password(request, user):
    admission(request, 'reauth:'+str(user.pk))
    value = request.data.get('current_password')
    require(isinstance(value, str) and len(value) <= 128 and user.check_password(value),
            'PASSWORD_INCORRECT', '현재 비밀번호가 올바르지 않습니다.', 403)


def admission(request, action):
    from django.conf import settings
    require(settings.MEMBER_PUBLIC_LIMIT_HOUR > 0, 'ACCOUNT_LIMITS_NOT_READY', '계정 요청 제한 설정이 필요합니다.', 503)
    require(services.limit(action+':'+request.META.get('REMOTE_ADDR', ''), maximum=settings.MEMBER_PUBLIC_LIMIT_HOUR),
            'RATE_LIMITED', '요청이 많습니다. 잠시 후 다시 시도해 주세요.', 429)


class Policy(Endpoint):
    def get(self, request):
        return Response(policy.documents())


class Availability(Endpoint):
    def post(self, request):
        admission(request, 'availability')
        name = policy.username(request.data.get('username'))
        return Response({'available': not get_user_model().objects.filter(username__iexact=name).exists(), 'username': name})


class Signup(Endpoint):
    def post(self, request):
        admission(request, 'signup')
        allowed = {'username', 'password', 'password_confirmation', 'nickname', 'email', 'email_consent',
                   'policy_version', 'terms_accepted', 'privacy_acknowledged', 'age_confirmed'}
        require(isinstance(request.data, dict) and not set(request.data)-allowed)
        profile = services.signup(request.data)
        pending = AccountToken.objects.filter(user=profile.user, purpose='email', consumed_at=None).exists()
        return Response({'created': True, 'email_pending': pending,
                         'email_deferred': bool(request.data.get('email_consent') and not pending),
                         'nickname': profile.nickname}, status=201)


class Profile(Endpoint):
    def get(self, request):
        return Response(services.profile_data(member(request)))

    def patch(self, request):
        profile = member(request)
        require(set(request.data) == {'nickname'})
        nick, key = policy.nickname(request.data['nickname'])
        try:
            with transaction.atomic():
                profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
                profile.nickname, profile.nickname_key = nick, key
                profile.save(update_fields=['nickname', 'nickname_key'])
        except IntegrityError:
            raise DomainError('NICKNAME_TAKEN', '이미 사용 중인 별명입니다.', 409) from None
        return Response(services.profile_data(profile))


class Email(Endpoint):
    def post(self, request):
        profile = member(request)
        require(request.data.get('email_consent') is True, 'AGREEMENT_REQUIRED', '복구 이메일 선택 동의가 필요합니다.')
        require(request.data.get('policy_version') == policy.documents()['version'], 'POLICY_CHANGED', '최신 안내를 확인해 주세요.', 409)
        with transaction.atomic():
            profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
            profile.user = get_user_model().objects.select_for_update().get(pk=profile.user_id)
            current_password(request, profile.user)
            services.start_email(profile.user, request.data.get('email'))
            profile.email_consent_at = timezone.now()
            profile.save(update_fields=['email_consent_at'])
            services.receipt(profile.user, 'email', request.data['policy_version'])
        return Response({'email_pending': True})

    def delete(self, request):
        profile = member(request)
        with transaction.atomic():
            profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
            profile.user = get_user_model().objects.select_for_update().get(pk=profile.user_id)
            current_password(request, profile.user)
            services.invalidate(profile.user)
            AccountMail.objects.filter(user=profile.user).delete()
            profile.email_ciphertext, profile.email_lookup = '', None
            profile.email_confirmed_at = profile.email_consent_at = None
            profile.save()
            services.receipt(profile.user, 'email', policy.documents()['version'], False)
        return Response({'email_registered': False})


class EmailVerify(Endpoint):
    def post(self, request):
        profile = member(request)
        services.verify_email(profile.user, request.data.get('code'))
        return Response({'email_verified': True})


class Password(Endpoint):
    def post(self, request):
        profile = member(request)
        with transaction.atomic():
            profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
            user = get_user_model().objects.select_for_update().get(pk=profile.user_id)
            current_password(request, user)
            policy.password(request.data.get('new_password'), request.data.get('password_confirmation'), user)
            require(not user.check_password(request.data['new_password']),
                    'PASSWORD_UNCHANGED', '새 비밀번호는 기존 비밀번호와 달라야 합니다.')
            user.set_password(request.data['new_password'])
            user.save(update_fields=['password'])
            services.invalidate(user, ['password', 'username'])
        # Existing sessions fail Django's session-auth hash check; rotate this authenticated session only.
        raw = raw_request(request)
        # DRF's unauthenticated user is intentionally None on this host; bind the
        # just-verified account before using Django's current-session rotation API.
        raw.user = user
        update_session_auth_hash(raw, user)
        return Response({'changed': True})


class Recovery(Endpoint):
    def post(self, request):
        services.recovery_request(request.data.get('purpose'), request.data.get('email'), request.META.get('REMOTE_ADDR', ''))
        return Response({'message': '등록·확인된 복구 이메일이 있다면 안내 메일을 발송합니다.'}, status=202)


class RecoveryComplete(Endpoint):
    def post(self, request):
        admission(request, 'recovery-complete')
        purpose = request.data.get('purpose')
        require(purpose in ('username', 'password'))
        result = services.consume_recovery(purpose, request.data.get('id'), request.data.get('token'),
            request.data.get('new_password'), request.data.get('password_confirmation'))
        return Response({'username': result} if purpose == 'username' else {'reset': True})


class Playlists(Endpoint):
    def get(self, request):
        profile = member(request)
        items = SavedPlaylist.objects.filter(user=profile.user).order_by('-created_at')[:100]
        return Response({'items': [{'id': row.pk, 'created_at': row.created_at, 'summary': row.snapshot.get('summary', ''),
                                  'track_count': len(row.snapshot.get('items', []))} for row in items]})

    def post(self, request):
        from backend.recommendation.models import Playlist
        from backend.recommendation.service import owned
        profile = member(request)
        source = owned(Playlist, str(profile.owner_id), request.data.get('playlist_id'))
        row, created = SavedPlaylist.objects.get_or_create(user=profile.user, source_id=source.pk,
            defaults={'snapshot': source.snapshot})
        return Response({'id': row.pk, 'saved': True}, status=201 if created else 200)


class PlaylistDetail(Endpoint):
    def get(self, request, pk):
        row = SavedPlaylist.objects.filter(user=member(request).user, pk=pk).first()
        require(row, 'NOT_FOUND', '저장한 선곡표를 찾을 수 없습니다.', 404)
        return Response({'id': row.pk, 'created_at': row.created_at, 'playlist': row.snapshot})

    def delete(self, request, pk):
        count, _ = SavedPlaylist.objects.filter(user=member(request).user, pk=pk).delete()
        require(count, 'NOT_FOUND', '저장한 선곡표를 찾을 수 없습니다.', 404)
        return Response({'deleted': True})


class Withdraw(Endpoint):
    def post(self, request):
        from .erasure import erase
        profile = member(request)
        require(request.data.get('confirmation') == '회원탈퇴', 'CONFIRMATION_REQUIRED', '회원탈퇴를 입력해 주세요.')
        with transaction.atomic():
            profile = MemberProfile.objects.select_for_update().get(pk=profile.pk)
            user = get_user_model().objects.select_for_update().get(pk=profile.user_id)
            current_password(request, user)
            erase(profile)
        logout(raw_request(request))
        return Response({'deleted': True})
