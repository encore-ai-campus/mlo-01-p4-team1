import time
from django.core.management.base import BaseCommand
from django.core.management import call_command
from backend.accounts.mail import deliver_one


class Command(BaseCommand):
    help = 'Process the shared account SMTP outbox; no recipients or payloads are logged.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        while True:
            for _ in range(options['limit']):
                if not deliver_one():
                    break
            call_command('cleanup_account_tokens', verbosity=0)
            if options['once']:
                return
            time.sleep(2)
