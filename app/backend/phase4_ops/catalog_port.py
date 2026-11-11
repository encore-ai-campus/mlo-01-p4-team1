from django.db.models import Q

from backend.music.models import Track
from backend.recommendation.models import YoutubeMapping

from .errors import require


def confirmed_video_filter(url):
    video_id = url.rsplit('=', 1)[1]
    mappings = YoutubeMapping.objects.filter(video_id=video_id).exclude(status='BLOCKED')
    mapped_ids = mappings.filter(Q(identity_confirmed=1) | Q(native_verified=1)).values('track_id')
    return Q(play_url=url) | Q(pk__in=mapped_ids)


def lookup(query):
    approved = Track.objects.filter(review__status='approved')
    if 'track_id' in query:
        return [{'track_id': value, 'eligible': True} for value in
                approved.filter(pk=query['track_id']).values_list('pk', flat=True)]
    by_name = Q(track_name__iexact=query['track_name'], artists_name__iexact=query['artist_name'])
    url = query['youtube_url']
    if url:
        exact_ids = set(approved.filter(confirmed_video_filter(url)).values_list('pk', flat=True))
    else:
        exact_ids = set()
    possible_ids = approved.filter(by_name).order_by('pk').values_list('pk', flat=True)[:20]
    return [{'track_id': value, 'confirmed': value in exact_ids, 'eligible': True}
            for value in sorted(exact_ids.union(possible_ids))]


def existing_track(track_id):
    rows = lookup({'track_id': track_id})
    matching = [row for row in rows if row['track_id'] == track_id and row.get('eligible') is True]
    require(len(matching) == 1, 'TRACK_NOT_AVAILABLE', '등록된 사용 가능 음악을 확인해 주세요.', 422)
    return matching[0]
