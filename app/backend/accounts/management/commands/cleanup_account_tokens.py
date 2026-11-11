from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db.models import Q
from backend.accounts.models import AccountToken, AccountLimit, AccountMail, PasskeyChallenge, PasskeyEnrollment


class Command(BaseCommand):
    help = 'Erase used/expired recovery data and expired passkey ceremonies.'

    def handle(self, *args, **options):
        now = timezone.now()
        AccountToken.objects.filter(Q(expires_at__lte=now) | Q(consumed_at__isnull=False)).delete()
        AccountMail.objects.filter(expires_at__lte=now).delete()
        # Rate-window metadata can be deleted once the one-hour window has ended.
        from datetime import timedelta
        AccountLimit.objects.filter(last_at__lt=now-timedelta(hours=1)).delete()
        PasskeyChallenge.objects.filter(expires_at__lte=now).delete()
        PasskeyEnrollment.objects.filter(expires_at__lte=now).delete()
