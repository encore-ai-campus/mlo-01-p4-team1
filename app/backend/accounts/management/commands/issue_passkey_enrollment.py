import hashlib
import secrets
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.db import transaction
from backend.accounts.models import PasskeyEnrollment, AdminPasskey
from backend.common.admin_roles import revoke_sessions


class Command(BaseCommand):
    help = 'Offline operator-only passkey enrollment. Output is a secret; never attach it to reports.'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('--replace-lost-keys', action='store_true')

    def handle(self, *args, **options):
        with transaction.atomic():
            user = get_user_model().objects.select_for_update().filter(username=options['username'], is_staff=True, is_active=True).first()
            if user is None:
                raise CommandError('active administrator required')
            if AdminPasskey.objects.filter(user=user).exists() and not options['replace_lost_keys']:
                raise CommandError('existing passkeys require explicit --replace-lost-keys after operator identity verification')
            if options['replace_lost_keys']:
                AdminPasskey.objects.filter(user=user).delete()
                revoke_sessions(user)
            raw = secrets.token_urlsafe(32)
            PasskeyEnrollment.objects.update_or_create(user=user, defaults={
                'digest':hashlib.sha256(raw.encode()).hexdigest(), 'expires_at':timezone.now()+timedelta(minutes=10)})
        self.stdout.write(raw)
