"""Attach an explicitly reviewed video to a promoted tagged music track."""
from django.conf import settings
from django.db import transaction

from backend.recommendation import youtube_registry
from backend.recommendation.errors import require
from backend.recommendation.youtube_database import review_actor
from .models import Track


@transaction.atomic
def register_tagged_youtube(
    track: Track, video_id: str, *, reviewed_duration_ms: int, actor: str, identity_and_embed_confirmed: bool,
):
    """Record the operator's review in the shared registry without changing existing approvals."""
    require(identity_and_embed_confirmed is True, 'REVIEW_INCOMPLETE',
            '원곡 일치와 YouTube 삽입 재생을 모두 확인해 주세요.')
    require(isinstance(actor, str) and actor.strip() not in {'', 'system'}, 'REVIEW_ACTOR_REQUIRED',
            '영상 검수 담당자를 확인해 주세요.', 403)
    require(settings.YOUTUBE_REGISTRY_BACKEND == 'database', 'POLICY_NOT_READY',
            '공유 영상 연결 저장소가 필요합니다.', 503)
    require(type(reviewed_duration_ms) is int and 0 < reviewed_duration_ms <= 86_400_000, 'INVALID_VIDEO_DURATION',
            '실제 재생에서 확인한 영상 길이를 밀리초로 입력해 주세요.')
    subject = {'track_id': track.pk, 'duration_ms': track.duration_ms}
    duration_seconds = reviewed_duration_ms / 1000
    youtube_registry.validate_iframe_review(subject, video_id, True, True, duration_seconds)

    current = youtube_registry.track_registry(track.pk)
    matching = next((mapping for mapping in current['mappings'] if mapping['video_id'] == video_id), None)
    if matching and matching['status'] == 'VERIFIED':
        return current
    require(not any(mapping['status'] == 'VERIFIED' for mapping in current['mappings']),
            'VIDEO_MAPPING_CONFLICT', '이미 다른 검수 완료 영상이 연결돼 있습니다.', 409)
    require(not matching or matching['status'] != 'BLOCKED',
            'VIDEO_MAPPING_BLOCKED', '차단된 영상은 기존 YouTube 검수에서 확인해 주세요.', 409)

    actor_token = review_actor.set(actor.strip())
    try:
        if not matching:
            result = youtube_registry.register_mappings([{
                'track_id': track.pk, 'video_url': video_id, 'expected_revision': current['revision'],
            }])
            current = result['items'][0]
        return youtube_registry.verify_mapping(
            subject, video_id, current['revision'], True, True, duration_seconds,
            note=matching['note'] if matching else '',
        )
    finally:
        review_actor.reset(actor_token)
