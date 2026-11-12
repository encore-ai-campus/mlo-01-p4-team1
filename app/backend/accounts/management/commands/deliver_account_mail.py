import time
from django.core.management.base import BaseCommand
from django.core.management import call_command
from backend.accounts.mail import deliver_one


MAINTENANCE_INTERVALS = {
    'cleanup_account_tokens': 60 * 60,
    'clearsessions': 24 * 60 * 60,
}


class Command(BaseCommand):
    help = 'Process the shared account SMTP outbox; no recipients or payloads are logged.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        next_run = dict.fromkeys(MAINTENANCE_INTERVALS, time.monotonic())
        while True:
            now = time.monotonic()
            for command, interval in MAINTENANCE_INTERVALS.items():
                if now >= next_run[command]:
                    call_command(command, verbosity=0)
                    next_run[command] = now + interval
            for _ in range(options['limit']):
                if not deliver_one():
                    break
            if options['once']:
                return
            time.sleep(2)
