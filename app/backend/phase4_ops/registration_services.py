from django.db import transaction
from django.db.models import Q

from backend.common.models import Review, ReviewChange
from backend.music.models import Album, Tag, Track, TrackTag
from backend.recommendation.policy import GENRES, THEMES

from . import serialization as out
from .catalog_port import confirmed_video_filter
from .errors import require
from .pipeline_services import locked_review_result, validate_dictionary, validate_tags
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
