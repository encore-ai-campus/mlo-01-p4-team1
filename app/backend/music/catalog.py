from django.db import transaction
from django.conf import settings
from pipeline.music.normalize import FIELDS
from backend.recommendation.errors import require
from backend.recommendation.policy import groups, THEMES, precompute_scores
from backend.recommendation.models import Catalog, OwnerLock
from backend.recommendation.catalog_storage import pack
from backend.recommendation.occasion import general_eligible
from music_playback.seasonality import screen_track
from .models import Track


def export_tracks(approved_only=False, source_sha256=None, track_ids=None):
    # Prefetch each link once; no genre x mood x instrument cartesian product.
    from django.db.models import Prefetch
    from .models import TrackTag
    links = TrackTag.objects.filter(review_status='approved',tag__is_active=True).select_related('tag')
    query=Track.objects.all()
    if approved_only:
        query=query.filter(review__status='approved')
    if source_sha256 is not None:
        query=query.filter(trackorigin__source_id=source_sha256)
    if track_ids is not None:
        query = query.filter(pk__in=track_ids)
    records = query.select_related('album').prefetch_related(
        Prefetch('tracktag_set', queryset=links, to_attr='approved_links'),
    ).order_by('track_id')
    for track in records.iterator(chunk_size=1000):
        tags = {'genre':[], 'mood':[], 'instrument':[]}
        for link in track.approved_links: tags[link.tag.tag_type].append(link.tag.tag_code)
        row = {key:getattr(track,key,None) for key in FIELDS}
        row.update({key:getattr(track,key) for key in ('raw_energy','raw_acousticness','raw_valence')})
        for key in ['album_id','album_name','album_release_date']: row[key] = getattr(track.album,key)
        row.update(genre=';'.join(sorted(tags['genre'])) or None,
                   mood=';'.join(THEMES.get(x,x) for x in sorted(tags['mood'])) or None,
                   main_inst=';'.join(sorted(tags['instrument'])) or None,
                   genre_group_codes=groups(tags['genre']),theme_codes=sorted(set(tags['mood']) & THEMES.keys()),
                   instrumental_review_value=track.instrumental_review_value,
                   instrumental_source='REVIEW' if track.instrumental_review_value is not None else 'PRESERVED',
                   cover_image_url=None)
        if track.instrumental_review_value is not None: row['instrumental'] = track.instrumental_review_value
        row.update(screen_track(row).as_row())
        yield row


def lock_publication():
    key = 'music-catalog:publication'
    OwnerLock.objects.get_or_create(owner_id=key)
    OwnerLock.objects.select_for_update().get(owner_id=key)


def store_catalog(version, source_ref, source_sha256, rows, *, is_test):
    require(source_ref and len(source_sha256) == 64, message='불변 원본 참조와 해시가 필요합니다.')
    require(rows, message='발행할 음악이 없습니다.')
    require(not Catalog.objects.filter(version=version).exists(), message='이미 발행된 버전입니다.', status=409)
    stored, digest = pack(rows)
    return Catalog.objects.create(version=version, tracks=stored, sha256=digest, source_ref=source_ref,
                                  source_sha256=source_sha256, is_test=is_test, ready=True)


def retained_reviewed_additions(is_test):
    from backend.phase4_ops.models import SongItem
    from backend.recommendation.worker import verified_catalog
    from .models import TaggedMusicCandidate

    published = SongItem.objects.filter(review_status='APPROVED', pipeline_status='REGISTERED').exclude(
        catalog_version='',
    ).values('track_id')
    from django.db.models import Q
    tagged = TaggedMusicCandidate.objects.exclude(track_id=None).values('track_id')
    approved_ids = set(Track.objects.filter(
        Q(pk__in=published) | Q(pk__in=tagged), review__status='approved',
    ).values_list('pk', flat=True))
    if not approved_ids:
        return []
    catalogs = Catalog.objects.defer('tracks').filter(ready=True, is_test=is_test)
    if is_test:
        catalogs = catalogs.exclude(source_ref__startswith='local-preview:youtube-playback-selection:')
    previous = catalogs.order_by('-publication__id').first()
    return [row for row in verified_catalog(previous) if row['track_id'] in approved_ids] if previous else []


@transaction.atomic
def publish(version, source_ref, source_sha256, is_test=False):
    """Publish a reviewed source while keeping separately published, still-approved additions."""
    lock_publication()
    rows = {row['track_id']: precompute_scores(row)
            for row in export_tracks(approved_only=settings.INTEGRATION_ENABLED,
                                     source_sha256=source_sha256 if settings.INTEGRATION_ENABLED else None)
            if general_eligible(row)}
    require(rows, message='발행할 음악이 없습니다.')
    rows.update((row['track_id'], row) for row in retained_reviewed_additions(is_test))
    return store_catalog(version, source_ref, source_sha256, list(rows.values()), is_test=is_test)
