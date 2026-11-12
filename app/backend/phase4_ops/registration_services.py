from django.conf import settings
from django.db import transaction
from django.db.models import Q

from backend.common.models import Review, ReviewChange
from backend.music.catalog import export_tracks, lock_publication, store_catalog
from backend.music.models import Album, Tag, Track, TrackTag
from backend.recommendation import youtube_registry
from backend.recommendation.inputs import DEFAULTS
from backend.recommendation.policy import GENRES, THEMES, eligible, precompute_scores
from backend.recommendation.service import current_catalog
from backend.recommendation.worker import verified_catalog
from pipeline.music.normalize import scaled

from . import serialization as out
from .catalog_port import confirmed_video_filter
from .errors import require
from .pipeline_services import locked_review_result, locked_task, validate_dictionary, validate_tags
from .transactions import lock, record
from .validation import expected_revision, fingerprint, shape, text, track_duration, youtube_url


@transaction.atomic
def finalize(actor, task_id, data):
    require('review_songs' in actor.capabilities, 'PERMISSION_DENIED', '곡 검수 권한이 필요합니다.', 403)
    fields = ['expected_revision', 'tags', 'album_name', 'duration_ms', 'explicit']
    shape(data, fields, fields)
    revision = expected_revision(data['expected_revision'])
    tags = data['tags']
    validate_tags(tags, minimum=1)
    validate_dictionary(tags)
    album_name = text(data['album_name'], '앨범명')
    duration = track_duration(data['duration_ms'])
    require(type(data['explicit']) is bool, 'INVALID_METADATA', '청소년 유해 표시 값을 확인해 주세요.', 422)

    task, receipt = locked_review_result(task_id, revision)
    item = task.item
    url = youtube_url(item.youtube_url)
    require(url and url == task.source_ref == receipt.candidate['source_ref'],
            'SOURCE_MISMATCH', '승인한 영상과 추론 결과의 연결을 확인해 주세요.', 409)
    video_id = url.rsplit('=', 1)[1]
    track_id = f'youtube:{video_id}'
    lock('song-source:' + fingerprint(url))
    require(not Track.objects.filter(Q(pk=track_id) | confirmed_video_filter(url)).exists(),
            'DUPLICATE_TRACK', '이미 등록된 영상입니다. 기존 음악을 확인해 주세요.', 409)

    track = Track.objects.create(
        track_id=track_id, track_name=item.track_name, artists_name=item.artist_name,
        album=Album.objects.create(album_name=album_name), duration_ms=duration,
        explicit=data['explicit'], play_url=url,
    )
    labels = {
        'genre': {**{code: pair[0] for code, pair in GENRES.items()},
                  'ROCK_PUNK_METAL': '록·펑크·메탈', 'FOLK_COUNTRY_BLUES': '포크·컨트리·블루스'},
        'mood': THEMES,
    }
    links = []
    for kind, codes in tags.items():
        for code in codes:
            tag, _ = Tag.objects.get_or_create(tag_type=kind, tag_code=code, defaults={
                'tag_name': labels[kind].get(code, code), 'is_active': True,
            })
            links.append(TrackTag(track=track, tag=tag, review_status='approved'))
    TrackTag.objects.bulk_create(links)
    review = Review.objects.create(track=track, revision=1, status='approved', reason='신청 곡 태깅 결과 최종 승인')
    ReviewChange.objects.create(track_id=track_id, actor_id=actor.account_id, revision=review.revision, snapshot={
        'review_status': review.status, 'reason': review.reason, 'genre_codes': tags['genre'], 'mood_codes': tags['mood'],
        'album_name': album_name, 'duration_ms': duration, 'explicit': track.explicit,
        'result_id': receipt.result_id, 'receipt_id': str(receipt.receipt_id),
    })

    item.track_id = track_id
    item.pipeline_status = 'REGISTERED'
    item.revision += 1
    item.save(update_fields=['track_id', 'pipeline_status', 'revision', 'updated_at'])
    task.status = 'REGISTERED'
    task.registration = {'track_id': track_id, 'tags': tags}
    task.revision += 1
    task.save(update_fields=['status', 'registration', 'revision', 'updated_at'])
    record('task', task.pk, actor.account_id, 'TRACK_REGISTERED', task.revision,
           track_id=track_id, receipt_id=str(receipt.receipt_id))
    return out.task(task, operator=True), 200


def recommendation_metadata(data):
    groups = data['genre_groups']
    require(type(groups) is list and 1 <= len(groups) <= len(GENRES) and
            all(type(code) is str and code in GENRES for code in groups) and len(set(groups)) == len(groups),
            'INVALID_GENRE_GROUPS', '검수한 서비스 장르를 선택해 주세요.', 422)
    features = {}
    evidence = {'genre_groups': sorted(groups)}
    if 'audio' in data:
        metrics = ('energy', 'acousticness', 'valence')
        audio = shape(data['audio'], (*metrics, 'source', 'mode'), (*metrics, 'source'))
        source = text(audio['source'], '음향 특성 출처')
        require(all(type(audio[name]) in (int, float) and 0 <= audio[name] <= 1 for name in metrics) and
                ('mode' not in audio or type(audio['mode']) is bool),
                'INVALID_AUDIO_FEATURES', '검수한 음향 특성은 0에서 1 사이의 값이어야 합니다.', 422)
        features.update({f'raw_{name}': str(audio[name]) for name in metrics})
        features.update(energy=scaled(audio['energy'], 10), acousticness=scaled(audio['acousticness'], 100))
        if 'mode' in audio:
            features['mode'] = audio['mode']
        evidence['audio'] = {**audio, 'source': source}
    return features, evidence


def stage_youtube_candidate(actor, track_id, url):
    # The local demo uses a separate SQLite file. Its existing review UI can register the URL.
    if settings.LOCAL_DEMO or settings.YOUTUBE_REGISTRY_BACKEND != 'database':
        return True
    from backend.recommendation.youtube_database import review_actor

    video_id = youtube_registry.normalize_video_url(url)
    current = youtube_registry.track_registry(track_id)
    existing = next((row for row in current['mappings'] if row['video_id'] == video_id), None)
    if existing:
        return not (existing['status'] == 'VERIFIED' and existing['identity_confirmed']
                    and existing['playback_observed'] and existing['duration_seconds'])
    token = review_actor.set(actor.account_id)
    try:
        youtube_registry.register_mappings([{
            'track_id': track_id, 'video_url': url, 'expected_revision': current['revision'],
        }], source='OPERATOR_URL')
    finally:
        review_actor.reset(token)
    return True


@transaction.atomic
def publish_registered(actor, task_id, data):
    require('review_songs' in actor.capabilities, 'PERMISSION_DENIED', '곡 검수 권한이 필요합니다.', 403)
    fields = ('expected_revision', 'genre_groups')
    shape(data, (*fields, 'audio', 'speed'), fields)
    revision = expected_revision(data['expected_revision'])
    features, evidence = recommendation_metadata(data)
    task = locked_task(task_id)
    require(task.revision == revision, 'REVISION_CONFLICT', '최신 검수 상태를 확인해 주세요.', 409)
    item = task.item
    receipt = getattr(task, 'receipt', None)
    require(task.is_current and task.status == item.pipeline_status == 'REGISTERED' and
            item.review_status == 'APPROVED' and item.track_id and not item.catalog_version and receipt,
            'INVALID_STATE', '추천 카탈로그에 아직 반영하지 않은 등록곡만 발행할 수 있습니다.', 409)
    url = youtube_url(item.youtube_url)
    require(url and url == task.source_ref == receipt.candidate['source_ref'],
            'SOURCE_MISMATCH', '승인 영상과 추론 결과의 연결을 확인해 주세요.', 409)

    lock_publication()
    previous = current_catalog()
    require(previous is not None, 'CATALOG_NOT_READY', '기존 추천 카탈로그를 먼저 발행해 주세요.', 409)
    rows = {row['track_id']: row for row in verified_catalog(previous)}
    legacy_row = rows.get(item.track_id)
    require(legacy_row is None or legacy_row.get('play_url') == url, 'SOURCE_MISMATCH',
            '기존 카탈로그의 곡과 승인 영상이 일치하는지 확인해 주세요.', 409)
    track = Track.objects.select_for_update().filter(pk=item.track_id, play_url=url).first()
    require(track is not None and Review.objects.filter(track=track, status='approved').exists(),
            'TRACK_NOT_AVAILABLE', '승인된 음악과 원본 영상을 확인해 주세요.', 409)
    for key, value in features.items():
        setattr(track, key, value)
    if features:
        track.save(update_fields=list(features))
    row = next(export_tracks(approved_only=True, track_ids=[track.pk]), None)
    require(row is not None, 'TRACK_NOT_AVAILABLE', '승인된 음악을 확인해 주세요.', 409)
    row['genre_group_codes'] = evidence['genre_groups']
    options = {**DEFAULTS, 'weather_influence_mode': 'OFF'}
    require(eligible(row, options, list(GENRES), (), []), 'TRACK_NOT_ELIGIBLE',
            '일반 선곡에 사용할 수 있는 곡인지 확인해 주세요.', 422)

    pending = stage_youtube_candidate(actor, track.pk, url)
    rows[track.pk] = precompute_scores(row)
    catalog = store_catalog(f'song-request:{task.pk}', previous.source_ref, previous.source_sha256,
                            list(rows.values()), is_test=previous.is_test)
    item.catalog_version = catalog.pk
    item.revision += 1
    item.save(update_fields=['catalog_version', 'revision', 'updated_at'])
    task.registration = {**(task.registration or {}), 'track_id': track.pk, 'catalog_version': catalog.pk,
                         'recommendation': evidence}
    task.revision += 1
    task.save(update_fields=['registration', 'revision', 'updated_at'])
    record('task', task.pk, actor.account_id, 'CATALOG_PUBLISHED', task.revision,
           catalog_version=catalog.pk, catalog_sha256=catalog.sha256, previous_version=previous.pk,
           receipt_id=str(receipt.receipt_id), receipt_sha256=receipt.payload_sha256, track_id=track.pk,
           recommendation=evidence)
    return {**out.task(task, operator=True), 'video_review_pending': pending}, 200
