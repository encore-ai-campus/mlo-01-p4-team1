"""Bounded SQL candidate lookup for an immutable, verified music catalog."""
from hashlib import sha256

from django.db import IntegrityError, transaction
from django.db.models import Case, Value, When

from . import policy
from .errors import DomainError, require
from .models import HourlyCatalogCandidate, HourlyCatalogIndex, HourlyCatalogTag
from .tagged_preview import EXTRA_TAGGED_GENRES, is_full_tagged_preview
from .youtube import VIDEO_ID
from .youtube_registry import normalize_video_url


# Increment when eligibility or the indexed attributes change.
INDEX_VERSION = 1
MAX_CANDIDATES = 1200
BUILD_BATCH_SIZE = 500
BASE_OPTIONS = {'weather_influence_mode': 'OFF', 'vocal_option': 'any'}


def _verified_video_id(row):
    video_id = row.get('youtube_video_id')
    if row.get('youtube_match_status') == 'VERIFIED' and isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id):
        return video_id
    return None


def catalog_video_id(row):
    """Extract a stable video ID for duplicate avoidance, without granting playback approval."""
    video_id = _verified_video_id(row)
    if video_id:
        return video_id
    url = row.get('play_url')
    if isinstance(url, str) and url.startswith('https://'):
        try:
            return normalize_video_url(url)
        except DomainError:
            return None
    return None


def _eligible_preview(row):
    video_id = row.get('youtube_video_id')
    return (isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id)
            and row.get('play_url') == f'https://www.youtube.com/watch?v={video_id}'
            and type(row.get('duration_ms')) is int and row['duration_ms'] > 0
            and all(isinstance(row.get(key), str) and row[key].strip()
                    for key in ('track_id', 'track_name', 'artists_name')))


def _materialize(index, rows, *, full_preview=False):
    genre_codes = policy.GENRES.keys() | EXTRA_TAGGED_GENRES.keys() if full_preview else policy.GENRES
    for start in range(0, len(rows), BUILD_BATCH_SIZE):
        candidates = []
        for position, row in enumerate(rows[start:start + BUILD_BATCH_SIZE], start=start):
            eligible = _eligible_preview(row) if full_preview else policy.eligible(row, BASE_OPTIONS, (), (), ())
            if not eligible:
                continue
            reviewed = row.get('instrumental_review_value')
            instrumental = reviewed if type(reviewed) is bool else row.get('instrumental') is True
            candidates.append(HourlyCatalogCandidate(
                catalog_index=index, position=position, track_id=row['track_id'], instrumental=instrumental,
                verified_youtube=_verified_video_id(row) is not None, payload=row,
            ))
        HourlyCatalogCandidate.objects.bulk_create(candidates)
        tags = []
        for candidate in candidates:
            for field, kind, known in (('genre_group_codes', 'genre', genre_codes),
                                       ('theme_codes', 'theme', policy.THEMES)):
                codes = candidate.payload.get(field)
                if isinstance(codes, (list, tuple)):
                    for code in {value for value in codes if isinstance(value, str) and value in known}:
                        tags.append(HourlyCatalogTag(catalog_index=index, candidate=candidate, kind=kind, code=code))
        HourlyCatalogTag.objects.bulk_create(tags)


def ensure_index(catalog):
    """Load a completed index or build it atomically from the verified snapshot."""
    require(catalog.ready, 'CATALOG_INVALID', '카탈로그 무결성 검증에 실패했어요.', 503)
    identity = {'catalog_id': catalog.pk, 'sha256': catalog.sha256, 'index_version': INDEX_VERSION}
    index = HourlyCatalogIndex.objects.filter(**identity).first()
    if index is not None:
        return index

    # Import lazily because the recommendation worker is an index consumer.
    from .worker import verified_catalog
    rows = verified_catalog(catalog)
    with transaction.atomic():
        try:
            with transaction.atomic():
                index = HourlyCatalogIndex.objects.create(**identity)
        except IntegrityError:
            # A competing insertion commits its completed index before this unique-key conflict.
            index = HourlyCatalogIndex.objects.filter(**identity).first()
            if index is None:
                raise
            return index
        _materialize(index, rows, full_preview=is_full_tagged_preview(catalog))
    return index


def _tagged(index, kind, codes):
    return HourlyCatalogTag.objects.filter(catalog_index=index, kind=kind, code__in=codes).values('candidate_id')


def _ranked_payloads(query, index, weather_themes, limit, pivot):
    ordering = []
    if weather_themes:
        ordering.append(Case(When(pk__in=_tagged(index, 'theme', weather_themes), then=Value(0)),
                             default=Value(1)))
    if pivot:
        ordering.append(Case(When(position__gte=pivot, then=Value(0)), default=Value(1)))
    return list(query.order_by(*ordering, 'position').values_list('payload', flat=True)[:limit])


def candidate_rows(catalog, *, genres=(), themes=(), weather_themes=(), excluded=(), instrumental_only=False,
                   limit=MAX_CANDIDATES, allow_theme_fallback=True, require_playable=False, rotation_seed=''):
    """Find canonical rows by OR genre and mood, with optional mood-only relaxation.

    Snapshot YouTube verification is optional. Current playback authorization belongs to the live registry.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f'Candidate limit must be between 1 and {MAX_CANDIDATES}.')

    index = ensure_index(catalog)
    query = HourlyCatalogCandidate.objects.filter(catalog_index=index)
    pivot = 0
    if rotation_seed and is_full_tagged_preview(catalog):
        # Rotate the bounded shortlist so the same first rows don't dominate unfiltered demo requests.
        last = query.order_by('-position').values_list('position', flat=True).first()
        if last is not None:
            pivot = int.from_bytes(sha256(rotation_seed.encode()).digest()[:8], 'big') % (last + 1)
    if genres:
        query = query.filter(pk__in=_tagged(index, 'genre', genres))
    if excluded:
        query = query.exclude(track_id__in=excluded)
    if instrumental_only:
        query = query.filter(instrumental=True)
    if require_playable:
        query = query.filter(verified_youtube=True)

    if not themes:
        return _ranked_payloads(query, index, weather_themes, limit, pivot)

    matching_themes = _tagged(index, 'theme', themes)
    rows = _ranked_payloads(query.filter(pk__in=matching_themes), index, weather_themes, limit, pivot)
    remaining = limit - len(rows)
    if allow_theme_fallback and remaining:
        rows.extend(_ranked_payloads(query.exclude(pk__in=matching_themes), index, weather_themes, remaining, pivot))
    return rows
