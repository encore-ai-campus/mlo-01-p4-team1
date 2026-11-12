"""Build a local test catalog from the consolidated tagging JSON staging rows."""
import json
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from backend.music.catalog import lock_publication, store_catalog
from backend.music.management.commands.import_tagged_music_catalog import SCHEMA, SHA256
from backend.music.models import TaggedMusicCandidate
from backend.recommendation.hourly_catalog import ensure_index
from backend.recommendation.models import Catalog, HourlyCatalogCandidate
from backend.recommendation.policy import GENRES, THEMES
from backend.recommendation.tagged_preview import (
    EXTRA_TAGGED_GENRES, TAGGED_PREVIEW_SOURCE_PREFIX, full_tagged_preview_version,
)
from music_playback.seasonality import screen_track


def label_codes(label, field, known):
    codes = label.get(field)
    if not isinstance(codes, list):
        return []
    return sorted({code for code in codes if isinstance(code, str) and code in known})


def project_track(data):
    # Prefer the later experiment's source labels. Predictions remain staged for review.
    label = max(data['labels'], key=lambda item: str(item.get('experiment', '')), default={})
    row = {
        'track_id': 'tagged:' + data['audio_sha256'],
        'audio_sha256': data['audio_sha256'],
        'track_name': data['title'],
        'artists_name': ', '.join(data['artists']),
        'album_name': data.get('album_name'),
        'duration_ms': data['duration_ms'],
        'cover_image_url': None,
        'genre_group_codes': label_codes(label, 'genre_tags', GENRES.keys() | EXTRA_TAGGED_GENRES.keys()),
        'theme_codes': label_codes(label, 'mood_tags', THEMES),
        'play_url': data['youtube_url'],
        'youtube_video_id': data['youtube_video_id'],
    }
    return {**row, **screen_track(row).as_row()}


class Command(BaseCommand):
    help = 'Index staged tagging JSON for local SQLite browser testing without approving tracks or video mappings.'

    def add_arguments(self, parser):
        parser.add_argument('--source-sha256', required=True)

    def handle(self, *args, **options):
        if not settings.LOCAL_DEMO or settings.INTEGRATION_ENABLED or connection.vendor != 'sqlite':
            raise CommandError('A non-integrated local SQLite demo is required')
        digest = options['source_sha256']
        if not SHA256.fullmatch(digest):
            raise CommandError('Expected a valid source SHA-256')
        staged = TaggedMusicCandidate.objects.filter(source_id=digest, source__schema=SCHEMA)
        rows = [project_track(data) for data in staged.order_by('audio_sha256')
                .values_list('record', flat=True).iterator(chunk_size=500)]
        if not rows:
            raise CommandError('No staged tagging rows for the local preview')

        version = full_tagged_preview_version(digest)
        source_ref = TAGGED_PREVIEW_SOURCE_PREFIX + digest
        with transaction.atomic():
            lock_publication()
            catalog = Catalog.objects.defer('tracks').filter(pk=version).first()
            if catalog is None:
                catalog = store_catalog(version, source_ref, digest, rows, is_test=True)
            elif not (catalog.is_test and catalog.source_sha256 == digest
                      and catalog.source_ref == source_ref):
                raise CommandError('Existing local preview version differs')
            index = ensure_index(catalog)
            indexed = HourlyCatalogCandidate.objects.filter(catalog_index=index).count()
            if indexed != len(rows):
                raise CommandError('The local preview index does not cover all staged tagging rows')
        self.stdout.write(json.dumps({
            'catalog_version': version, 'source_sha256': digest, 'staged': len(rows), 'indexed': indexed,
            'missing_album': sum(not row['album_name'] for row in rows),
            'missing_genre': sum(not row['genre_group_codes'] for row in rows),
            'occasion_statuses': dict(Counter(row['occasion_status'] for row in rows)), 'is_test': True,
        }, sort_keys=True))
