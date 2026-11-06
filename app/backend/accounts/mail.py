"""Shared SMTP outbox with expiring leases and stable Message-ID on retries."""
import uuid
from datetime import timedelta
from urllib.parse import urlsplit
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from backend.recommendation.errors import require
from .crypto import unseal
from .models import AccountMail, MemberProfile


def deliver_one():
    require(settings.EMAIL_HOST and settings.DEFAULT_FROM_EMAIL and
        (settings.MEMBER_LOCAL_PREVIEW or settings.EMAIL_USE_SSL != settings.EMAIL_USE_TLS),
        'SMTP_NOT_READY', 'TLS SMTP 연결 설정이 필요합니다.', 503)
    now = timezone.now()
    candidate = AccountMail.objects.filter(next_attempt_at__lte=now).filter(
        Q(state='PENDING') | Q(state='SENDING', lease_until__lt=now)).order_by('created_at').values('pk', 'user_id').first()
    if candidate is None:
        return False
    with transaction.atomic():
        profile = MemberProfile.objects.select_for_update().filter(user_id=candidate['user_id'], email_consent_at__isnull=False).first()
        row = AccountMail.objects.select_for_update(skip_locked=not settings.LOCAL_DEMO).filter(pk=candidate['pk']).filter(
            Q(state='PENDING') | Q(state='SENDING', lease_until__lt=now)).first()
        if row is None:
            return False
        # Lock the same profile as withdrawal/email revocation and recheck consent/expiry.
        if profile is None or row.expires_at <= now or (row.token_id and (row.token.consumed_at or row.token.expires_at <= now)):
            row.delete()
            return True
        row.state, row.lease, row.lease_until = 'SENDING', uuid.uuid4(), now+timedelta(seconds=30)
        row.attempts += 1
        row.save()
        payload, identifier, lease = unseal(row.payload), row.pk, row.lease
    try:
        domain = urlsplit(settings.MEMBER_PUBLIC_ORIGIN).hostname or 'localhost'
        sent = EmailMessage(payload['subject'], payload['text'], settings.DEFAULT_FROM_EMAIL, [payload['to']],
                     headers={'Message-ID':f'<account-{identifier}@{domain}>'}).send(fail_silently=False)
        if sent != 1:
            raise RuntimeError('smtp_not_accepted')
    except Exception as error:
        # Never record SMTP messages/addresses, token URLs or exception strings.
        AccountMail.objects.filter(pk=identifier, lease=lease).update(state='PENDING', lease=None, lease_until=None,
            next_attempt_at=timezone.now()+timedelta(seconds=min(300, 2**min(row.attempts, 8))),
            last_error=type(error).__name__[:64])
        return True
    AccountMail.objects.filter(pk=identifier, lease=lease).delete()
    return True
