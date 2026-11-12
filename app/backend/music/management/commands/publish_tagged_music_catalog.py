"""Publish reviewed tracks from a staged tagging JSON into a new catalog version."""
import json
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from backend.common.models import Review, ReviewChange, TrackOrigin
from backend.music.catalog import export_tracks, lock_publication, store_catalog
from backend.music.models import Album, Tag, TaggedMusicCandidate, Track, TrackTag
from backend.music.tagged_playback import register_tagged_youtube
from backend.phase4_ops.registration_services import recommendation_metadata
from backend.recommendation.errors import DomainError
from backend.recommendation.inputs import DEFAULTS
from backend.recommendation.models import YoutubeMapping
from backend.recommendation.policy import GENRES, THEMES, eligible, groups, precompute_scores
from backend.recommendation.service import current_catalog
from backend.recommendation.worker import verified_catalog

from .import_tagged_music_catalog import SCHEMA, SHA256, nonempty, watch_url


def read_reviews(path):
    try:
        reviews = json.loads(Path(path).read_bytes())
    except (OSError, ValueError, UnicodeError) as error:
        raise CommandError('Invalid or unreadable review JSON') from error
    if not isinstance(reviews, list) or not reviews:
        raise CommandError('Review JSON must contain a nonempty list of reviewed tracks')
    seen = set()
    for number, review in enumerate(reviews, 1):
        required = {'audio_sha256', 'genre_groups', 'theme_codes', 'explicit',
                    'reviewed_duration_ms', 'identity_and_embed_confirmed'}
        optional = {'album_name', 'audio', 'instrumental', 'speed'}
        if (not isinstance(review, dict) or not required <= review.keys()
                or review.keys() - required - optional):
            raise CommandError(f'Missing or unexpected review fields at row {number}')
        digest = review['audio_sha256']
        themes = review['theme_codes']
        if (not isinstance(digest, str) or not SHA256.fullmatch(digest) or digest in seen
                or not isinstance(themes, list) or not all(isinstance(t, str) and t in THEMES for t in themes)
                or len(set(themes)) != len(themes) or type(review['explicit']) is not bool
                or ('instrumental' in review and type(review['instrumental']) is not bool)
                or ('album_name' in review and not nonempty(review['album_name']))
                or review['identity_and_embed_confirmed'] is not True
                or type(review['reviewed_duration_ms']) is not int or review['reviewed_duration_ms'] <= 0):
            raise CommandError(f'Invalid reviewed fields at row {number}')
        seen.add(digest)
    return reviews


def add_approved_tags(track, evidence, themes):
    for code in evidence['genre_groups']:
        name, aliases = GENRES[code]
        alias = next(value for value in aliases.split() if groups([value]) == [code])
        link_approved(track, 'genre', alias, name)
    for code in themes:
        link_approved(track, 'mood', code, THEMES[code])


def link_approved(track, kind, code, name):
    tag, _ = Tag.objects.get_or_create(tag_type=kind, tag_code=code,
                                      defaults={'tag_name': name, 'is_active': True})
    if not tag.is_active:
        raise CommandError(f'Service tag is inactive: {kind}/{code}')
    TrackTag.objects.create(track=track, tag=tag, review_status='approved')


def publish_candidate(candidate, review, reviewer, version):
    data = candidate.record
    features, evidence = recommendation_metadata(review)
    album_name = review.get('album_name') or data.get('album_name')
    track_id = f'tagged:{candidate.audio_sha256}'
    url = watch_url(candidate.video_id)
    if candidate.track_id or Track.objects.filter(pk=track_id).exists():
        raise CommandError(f'This recording is already registered: {candidate.audio_sha256}')
    if (Track.objects.filter(play_url=url).exists()
            or YoutubeMapping.objects.filter(video_id=candidate.video_id, status='VERIFIED')
                .exclude(track_id=track_id).exists()):
        raise CommandError(f'YouTube URL already belongs to a track: {candidate.video_id}')
    if not album_name:
        raise CommandError(f'Confirm the album name for {candidate.audio_sha256}')
    track = Track.objects.create(
        track_id=track_id, track_name=data['title'], artists_name=', '.join(data['artists']),
        album=Album.objects.create(album_name=album_name), duration_ms=data['duration_ms'],
        explicit=review['explicit'], play_url=url, instrumental=review.get('instrumental'),
        instrumental_review_value=review.get('instrumental'), **features,
    )
    TrackOrigin.objects.create(track=track, source=candidate.source)
    approval = Review.objects.create(track=track, revision=1, status='approved', reason='tagged_catalog_review')
    add_approved_tags(track, evidence, review['theme_codes'])
    candidate.track = track
    candidate.save(update_fields=['track'])
    row = next(export_tracks(approved_only=True, track_ids=[track.pk]))
    options = {**DEFAULTS, 'weather_influence_mode': 'OFF'}
    if not eligible(row, options, list(GENRES), (), []):
        raise CommandError(f'Track is ineligible for general recommendation: {candidate.audio_sha256}')
    register_tagged_youtube(track, candidate.video_id, reviewed_duration_ms=review['reviewed_duration_ms'],
                            actor=str(reviewer.pk), identity_and_embed_confirmed=review['identity_and_embed_confirmed'])
    ReviewChange.objects.create(
        track_id=track.pk, actor=reviewer, revision=approval.revision,
        snapshot={'source_sha256': candidate.source_id, 'catalog_version': version, 'review': review},
    )
    return precompute_scores(row)


@transaction.atomic
def publish_reviews(source_digest, version, reviews, reviewer):
    lock_publication()
    previous = current_catalog()
    rows = {row['track_id']: row for row in verified_catalog(previous)} if previous else {}
    hashes = [review['audio_sha256'] for review in reviews]
    candidates = TaggedMusicCandidate.objects.select_for_update().select_related('source').filter(
        source_id=source_digest, source__schema=SCHEMA, audio_sha256__in=hashes,
    )
    by_hash = {candidate.audio_sha256: candidate for candidate in candidates}
    if len(by_hash) != len(hashes):
        raise CommandError('One or more reviewed tracks are missing from the staged source')
    for review in reviews:
        row = publish_candidate(by_hash[review['audio_sha256']], review, reviewer, version)
        rows[row['track_id']] = row
    return store_catalog(version, 'music-source:'+source_digest, source_digest,
                         list(rows.values()), is_test=settings.LOCAL_DEMO)


class Command(BaseCommand):
    help = 'Publish a reviewed list from the staged tagging JSON. Existing catalog tracks are retained.'

    def add_arguments(self, parser):
        parser.add_argument('reviews', help='JSON array of reviews: audio_sha256, genre_groups, theme_codes, '
                            'explicit, reviewed_duration_ms, identity_and_embed_confirmed; optionally album_name, '
                            'instrumental and audio (energy, acousticness, valence, source and optional mode)')
        parser.add_argument('--source-sha256', required=True)
        parser.add_argument('--catalog-version', required=True)
        parser.add_argument('--reviewer', required=True, help='Active Django username with music review permission')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        if not options['apply']:
            raise CommandError('Publication requires explicit --apply')
        digest = options['source_sha256']
        version = options['catalog_version']
        if not SHA256.fullmatch(digest) or not nonempty(version):
            raise CommandError('A valid source SHA-256 and catalog version are required')
        user_model = get_user_model()
        reviewer = user_model.objects.filter(**{user_model.USERNAME_FIELD: options['reviewer']}, is_active=True).first()
        if reviewer is None or not reviewer.has_perm('common.manage_music'):
            raise CommandError('An active reviewer with common.manage_music permission is required')
        reviews = read_reviews(options['reviews'])
        try:
            catalog = publish_reviews(digest, version, reviews, reviewer)
        except DomainError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps({'catalog_version': catalog.pk, 'published_tracks': len(reviews)}))
