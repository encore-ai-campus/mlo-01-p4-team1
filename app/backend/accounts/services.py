import hmac
import hashlib
import secrets
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from backend.recommendation.errors import DomainError, require
from . import crypto, policy
from .models import MemberProfile, PolicyReceipt, AccountToken, AccountMail, AccountLimit


def limit(subject, *, interval=0, maximum=0):
    """Shared SQL row lock, not a per-Control-server in-memory counter."""
    now = timezone.now()
    identifier = crypto.digest('rate', subject)
    with transaction.atomic():
        AccountLimit.objects.get_or_create(pk=identifier, defaults={'window_at': now, 'last_at': now-timedelta(hours=1)})
        row = AccountLimit.objects.select_for_update().get(pk=identifier)
        if now - row.window_at >= timedelta(hours=1):
            row.window_at, row.count = now, 0
        allowed = (not interval or (now-row.last_at).total_seconds() >= interval) and (not maximum or row.count < maximum)
        if allowed:
            row.last_at, row.count = now, row.count + 1
            row.save()
        return allowed


def mail_limits():
    value = settings.MEMBER_MAIL_LIMIT_HOUR
    require(value > 0, 'MAIL_POLICY_NOT_READY', '메일 발송 제한 설정이 필요합니다.', 503)
    return value


def queue_mail(user, address, kind, subject, text, token=None):
    require(token is not None or settings.MEMBER_SECURITY_MAIL_SECONDS > 0,
            'MAIL_POLICY_NOT_READY', '보안 안내 메일의 보유 설정이 필요합니다.', 503)
    return AccountMail.objects.create(user=user, token=token, kind=kind,
        payload=crypto.seal({'to': address, 'subject': subject, 'text': text}), next_attempt_at=timezone.now(),
        expires_at=token.expires_at if token else timezone.now()+timedelta(seconds=settings.MEMBER_SECURITY_MAIL_SECONDS))


def receipt(user, kind, version, accepted=True):
    document = settings.MEMBER_POLICY_DOCUMENTS.get(kind,
        '만 14세 이상 확인' if kind == 'age14' else '')
    return PolicyReceipt.objects.create(user=user, kind=kind, version=version, accepted=accepted,
        document_sha256=hashlib.sha256(document.encode()).hexdigest())


def invalidate(user, purposes=None):
    tokens = AccountToken.objects.filter(user=user, consumed_at=None)
    if purposes:
        tokens = tokens.filter(purpose__in=purposes)
    ids = list(tokens.values_list('pk', flat=True))
    AccountMail.objects.filter(token_id__in=ids).delete()
    tokens.update(consumed_at=timezone.now(), email_ciphertext='')


def profile_data(profile):
    address = crypto.unseal(profile.email_ciphertext) if profile.email_ciphertext else ''
    local, _, domain = address.partition('@')
    pending = AccountToken.objects.filter(user=profile.user, purpose='email', consumed_at=None,
        expires_at__gt=timezone.now()).order_by('-created_at').first()
    return {'username': profile.user.username, 'nickname': profile.nickname, 'store_type': profile.store_type,
            'email_registered': bool(address), 'email_masked': (local[:1]+'***@'+domain) if address else '',
            'email_verified': bool(profile.email_confirmed_at), 'email_pending': bool(pending),
            'email_pending_until': pending.expires_at.isoformat() if pending else None}


def signup(body):
    policy.consent(body)
    name = policy.username(body.get('username'))
    user = get_user_model()(username=name)
    policy.password(body.get('password'), body.get('password_confirmation'), user)
    supplied = body.get('nickname', '')
    require(isinstance(supplied, str))
    chosen = policy.nickname(supplied) if supplied else None
    # A value without optional consent is intentionally discarded before any DB/queue write.
    address = policy.email(body.get('email', '')) if body.get('email_consent') is True else None
    if address:
        mail_limits()
        crypto.seal(address)  # Validate configured keys before creating any data.
    user.set_password(body['password'])
    require(not get_user_model().objects.filter(username__iexact=name).exists(),
            'USERNAME_TAKEN', '이미 사용 중인 아이디입니다.', 409)
    for _ in range(100):
        nick, nick_key = chosen or policy.nickname('회원' + f'{secrets.randbelow(10000):04d}')
        try:
            with transaction.atomic():
                user.pk = None
                user.save(force_insert=True)
                profile = MemberProfile.objects.create(user=user, nickname=nick, nickname_key=nick_key,
                    email_consent_at=timezone.now() if address else None)
                for kind in ('terms', 'privacy_notice', 'age14'):
                    receipt(user, kind, body['policy_version'])
                if address:
                    receipt(user, 'email', body['policy_version'])
                    start_email(user, address, allow_deferred=True)
                return profile
        except IntegrityError:
            if get_user_model().objects.filter(username__iexact=name).exists():
                raise DomainError('USERNAME_TAKEN', '이미 사용 중인 아이디입니다.', 409) from None
            if chosen:
                raise DomainError('NICKNAME_TAKEN', '이미 사용 중인 별명입니다.', 409) from None
    raise DomainError('NICKNAME_UNAVAILABLE', '기본 별명 생성이 지연되고 있습니다. 별명을 직접 입력해 주세요.', 409)


def start_email(user, address, allow_deferred=False):
    address = policy.email(address)
    lookup = crypto.digest('email', address)
    maximum = mail_limits()
    user_allowed = limit('email-send:'+str(user.pk), interval=60, maximum=maximum)
    address_allowed = limit('email-address:'+address, interval=60, maximum=maximum)
    if not (user_allowed and address_allowed):
        if allow_deferred:
            return False  # Optional mail throttling must not roll back a basic member signup.
        raise DomainError('RATE_LIMITED', '잠시 후 이메일 확인을 다시 요청해 주세요.', 429)
    invalidate(user, ['email'])
    code = f'{secrets.randbelow(1000000):06d}'
    row = AccountToken(user=user, purpose='email', email_ciphertext=crypto.seal(address), email_lookup=lookup,
                       expires_at=timezone.now()+timedelta(minutes=5))
    row.digest = crypto.digest('email:'+str(row.pk), code)
    row.save()
    queue_mail(user, address, 'email', '복구 이메일 확인',
        '복구 이메일 확인 코드는 '+code+' 입니다. 5분 동안 유효하며 가입·로그인에는 확인이 필요하지 않습니다.', row)
    return True


def verify_email(user, code):
    failure = False
    with transaction.atomic():
        profile = MemberProfile.objects.select_for_update().get(user=user)
        row = AccountToken.objects.select_for_update().filter(user=user, purpose='email', consumed_at=None).order_by('-created_at').first()
        require(row and row.expires_at > timezone.now() and row.attempts < 5,
                'INVALID_CODE', '확인 코드가 만료되었거나 사용할 수 없습니다.')
        row.attempts += 1
        failure = not (isinstance(code, str) and len(code) == 6 and code.isascii() and code.isdigit()
                       and hmac.compare_digest(row.digest, crypto.digest('email:'+str(row.pk), code)))
        row.save(update_fields=['attempts'])
        if not failure:
            require(not MemberProfile.objects.exclude(pk=profile.pk).filter(email_lookup=row.email_lookup).exists(),
                    'EMAIL_UNAVAILABLE', '이 이메일은 복구 주소로 사용할 수 없습니다.', 409)
            old = crypto.unseal(profile.email_ciphertext) if profile.email_ciphertext else None
            new = crypto.unseal(row.email_ciphertext)
            profile.email_ciphertext, profile.email_lookup = row.email_ciphertext, row.email_lookup
            profile.email_confirmed_at = timezone.now()
            try:
                with transaction.atomic():
                    profile.save()
            except IntegrityError:
                raise DomainError('EMAIL_UNAVAILABLE', '이 이메일은 복구 주소로 사용할 수 없습니다.', 409) from None
            invalidate(user)
            if old and old != new:
                queue_mail(user, old, 'security', '복구 이메일 변경 안내',
                    '계정의 복구 이메일이 변경되었습니다. 본인이 변경하지 않았다면 서비스의 보안 문의 창구로 연락해 주세요.')
    # Raise after commit so failed attempt counts cannot roll back.
    if failure:
        raise DomainError('INVALID_CODE', '확인 코드가 올바르지 않습니다.')


def recovery_request(purpose, address, remote):
    require(purpose in ('username', 'password'))
    from urllib.parse import urlsplit
    origin = urlsplit(settings.MEMBER_PUBLIC_ORIGIN)
    require(origin.hostname and not origin.username and not origin.path and not origin.query and not origin.fragment
            and (origin.scheme == 'https' or (settings.MEMBER_LOCAL_PREVIEW and origin.scheme == 'http'))
            and settings.MEMBER_RECOVERY_IP_LIMIT_HOUR > 0,
            'RECOVERY_NOT_READY', '계정 복구 설정을 준비하고 있습니다.', 503)
    try:
        normalized = policy.email(address)
    except DomainError:
        normalized = ''
    allowed = limit('recovery-address:'+normalized, interval=60, maximum=mail_limits())
    allowed = limit('recovery-ip:'+remote, maximum=settings.MEMBER_RECOVERY_IP_LIMIT_HOUR) and allowed
    lookup = crypto.digest('email', normalized)
    # Dummy encryption also follows the miss path; SMTP is never in request latency.
    encrypted = crypto.seal(normalized)
    with transaction.atomic():
        profile = MemberProfile.objects.select_for_update().filter(email_lookup=lookup, user__is_active=True, user__is_staff=False).first()
        if profile is None or not allowed:
            return
        invalidate(profile.user, [purpose])
        raw = secrets.token_urlsafe(32)
        row = AccountToken(user=profile.user, purpose=purpose, email_lookup=lookup, email_ciphertext=encrypted,
                           expires_at=timezone.now()+timedelta(minutes=10))
        row.digest = crypto.digest(purpose+':'+str(row.pk), raw)
        row.save()
        # Fragment prevents token transport in HTTP URL/access logs and referrer headers.
        recovery_path='/platform/account/recovery' if getattr(settings,'PLATFORM_ENABLED',False) and settings.MEMBER_LOCAL_PREVIEW else '/account/recovery'
        link = settings.MEMBER_PUBLIC_ORIGIN.rstrip('/')+recovery_path+'#'+purpose+'/'+str(row.pk)+'/'+raw
        subject=('메인 플랫폼 ' if getattr(settings,'PLATFORM_ENABLED',False) and settings.MEMBER_LOCAL_PREVIEW else '')+('아이디 안내' if purpose == 'username' else '비밀번호 재설정')
        queue_mail(profile.user, normalized, purpose, subject,
                   '요청하신 계정 복구 링크입니다. 10분 동안 한 번만 사용할 수 있습니다.\n'+link, row)


def consume_recovery(purpose, identifier, raw, new_password=None, confirmation=None):
    with transaction.atomic():
        # Lock profile first in every email/token mutation to avoid cross-server races.
        try:
            owner = AccountToken.objects.get(pk=identifier).user_id
        except (AccountToken.DoesNotExist, ValueError, ValidationError):
            raise DomainError('INVALID_TOKEN', '복구 링크가 만료되었거나 사용할 수 없습니다.') from None
        profile = MemberProfile.objects.select_for_update().filter(user_id=owner, user__is_active=True, user__is_staff=False).first()
        row = AccountToken.objects.select_for_update().get(pk=identifier)
        valid = profile and row.purpose == purpose and row.consumed_at is None and row.expires_at > timezone.now()
        require(valid and isinstance(raw, str) and len(raw) <= 128 and
                hmac.compare_digest(row.digest, crypto.digest(purpose+':'+str(row.pk), raw)) and
                row.email_lookup == profile.email_lookup, 'INVALID_TOKEN', '복구 링크가 만료되었거나 사용할 수 없습니다.')
        user = get_user_model().objects.select_for_update().get(pk=owner)
        if purpose == 'password':
            policy.password(new_password, confirmation, user)
            require(not user.check_password(new_password), 'PASSWORD_UNCHANGED', '새 비밀번호는 기존 비밀번호와 달라야 합니다.')
            user.set_password(new_password)
            user.save(update_fields=['password'])
            from backend.common.admin_roles import revoke_sessions
            revoke_sessions(user)
            invalidate(user, ['password', 'username'])
        else:
            row.consumed_at = timezone.now()
            row.email_ciphertext = ''
            row.save()
            AccountMail.objects.filter(token=row).delete()
        return user.username if purpose == 'username' else None
