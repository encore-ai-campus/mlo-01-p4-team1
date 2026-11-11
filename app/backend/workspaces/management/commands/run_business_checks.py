import time
from django.core.management.base import BaseCommand
from backend.workspaces.authorization import enabled
from backend.workspaces.worker import run_one


class Command(BaseCommand):
    help = 'Run local synthetic business check jobs; no external provider calls.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        enabled()
        while True:
            ran = run_one()
            if options['once']:
                break
            if not ran:
                time.sleep(.5)
