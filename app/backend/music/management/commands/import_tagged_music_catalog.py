"""Load the consolidated tagging JSON into the music review queue."""
import hashlib
import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.common.models import MusicSource
from backend.common.music_archive import store as archive_source
from backend.music.models import TaggedMusicCandidate
from backend.recommendation.errors import DomainError
from .import_music_catalog_source import stored_source


SCHEMA = 'muther-tagged-tracks.v1'
SHA256 = re.compile(r'[0-9a-f]{64}')
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')


def watch_url(video_id):
    return f'https://www.youtube.com/watch?v={video_id}'


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def checked_tracks(raw):
    try:
        document = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise CommandError('Invalid tagging JSON') from error
    if not isinstance(document, dict) or document.get('schema_version') != SCHEMA:
        raise CommandError(f'Expected {SCHEMA}')
    rows = document.get('tracks')
    if not isinstance(rows, list) or not rows:
        raise CommandError('tracks must be a nonempty list')
    hashes, videos = set(), set()
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise CommandError(f'Invalid track at row {number}')
        digest = row.get('audio_sha256')
        video_id = row.get('youtube_video_id')
        artists = row.get('artists')
        length = row.get('duration_ms')
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or digest in hashes:
            raise CommandError(f'Invalid or duplicate audio SHA-256 at row {number}')
        if (not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id)
                or video_id in videos or row.get('youtube_url') != watch_url(video_id)):
            raise CommandError(f'Invalid or duplicate YouTube reference at row {number}')
        if (not nonempty(row.get('title')) or not isinstance(artists, list) or not artists
                or not all(map(nonempty, artists)) or type(length) is not int or not 0 < length <= 2147483647
                or (row.get('album_name') is not None and not nonempty(row['album_name']))):
            raise CommandError(f'Invalid track metadata at row {number}')
        for key in ('labels', 'predictions'):
            entries = row.get(key)
            if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
                raise CommandError(f'Invalid {key} at row {number}')
        hashes.add(digest)
        videos.add(video_id)
    return rows


class Command(BaseCommand):
    help = 'Validate or stage a muther-tagged-tracks.v1 JSON file. Use --apply to write pending candidates.'

    def add_arguments(self, parser):
        parser.add_argument('source')
        parser.add_argument('--expected-sha256', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        path = Path(options['source']).resolve()
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise CommandError('Tagging JSON source not found or unreadable') from error
        digest = hashlib.sha256(raw).hexdigest()
        if digest != options['expected_sha256']:
            raise CommandError('Tagging JSON SHA-256 differs from expected source')
        rows = checked_tracks(raw)
        summary = {
            'source_sha256': digest,
            'tracks': len(rows),
            'with_predictions': sum(bool(row['predictions']) for row in rows),
            'without_album': sum(not row.get('album_name') for row in rows),
            'applied': options['apply'],
        }
        if options['apply']:
            try:
                archived = stored_source(path, digest, suffix='.json')
                archive = archive_source(archived, digest, suffix='.json')
            except DomainError as error:
                raise CommandError(str(error)) from error
            with transaction.atomic():
                source, _ = MusicSource.objects.get_or_create(sha256=digest, defaults={
                    'schema': SCHEMA, 'raw_path': str(archived), 'archive': archive,
                })
                source = MusicSource.objects.select_for_update().get(pk=source.pk)
                if source.schema != SCHEMA:
                    raise CommandError('Registered source schema differs')
                count = TaggedMusicCandidate.objects.filter(source=source).count()
                if not count:
                    TaggedMusicCandidate.objects.bulk_create([
                        TaggedMusicCandidate(source=source, audio_sha256=row['audio_sha256'],
                                             video_id=row['youtube_video_id'], record=row)
                        for row in rows
                    ], batch_size=500)
                elif count != len(rows):
                    raise CommandError('Existing staged candidate count differs from the source')
        self.stdout.write(json.dumps(summary, sort_keys=True))
