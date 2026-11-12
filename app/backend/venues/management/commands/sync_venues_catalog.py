"""Synchronize venue CSV catalog to DB mirror and/or export Track 2 S3 raw snapshot."""
import json

from django.core.management.base import BaseCommand, CommandError

from backend.venues.services.lakehouse_sync import (
    export_lakehouse_snapshot,
    sync_catalog_to_db,
)


class Command(BaseCommand):
    help = 'Sync CSV venue catalog to VenueCatalogRecord DB and/or export Track 2 S3 Data Lakehouse raw snapshot.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--target',
            choices=['db', 'lakehouse', 'all'],
            default='all',
            help='Sync target: DB mirror, S3/local lakehouse raw snapshot, or both.',
        )
        parser.add_argument('--gu', action='append', dest='gus', help='Optional district filter (repeatable).')
        parser.add_argument('--dataset-version', default=None, help='Track 2 dataset version (default env or v1).')
        parser.add_argument('--output-dir', default='', help='Local directory root for raw/venues/<dataset_version>/ output.')
        parser.add_argument('--s3-bucket', default='', help='Optional S3 raw bucket override.')
        parser.add_argument('--limit', type=int, default=None, help='Optional max venues cap for DB sync.')
        parser.add_argument('--no-outbox', action='store_true', help='Skip VenueOutbox export in lakehouse snapshot.')
        parser.add_argument('--dry-run', action='store_true', help='Scan and validate without writing DB or files/S3.')

    def handle(self, *args, **options):
        target = options['target']
        gus = options.get('gus') or None
        dataset_version = options.get('dataset_version')
        dry_run = bool(options.get('dry_run'))

        summary = {'target': target, 'dry_run': dry_run}
        try:
            if target in ('db', 'all'):
                summary['db'] = sync_catalog_to_db(
                    gus=gus,
                    dataset_version=dataset_version,
                    dry_run=dry_run,
                    limit=options.get('limit'),
                )
            if target in ('lakehouse', 'all'):
                summary['lakehouse'] = export_lakehouse_snapshot(
                    gus=gus,
                    dataset_version=dataset_version,
                    output_dir=options.get('output_dir') or None,
                    s3_bucket=options.get('s3_bucket') or None,
                    include_outbox=not bool(options.get('no_outbox')),
                    dry_run=dry_run,
                )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(summary, ensure_ascii=False))
