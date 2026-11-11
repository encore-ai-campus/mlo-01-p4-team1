import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit

from .errors import require


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def shape(value, allowed, required=()):
    require(type(value) is dict and set(value) <= set(allowed) and set(required) <= set(value),
            'INVALID_INPUT', '입력 항목을 확인해 주세요.')
    return value


def text(value, name, maximum=300, optional=False):
    require(type(value) is str, 'INVALID_INPUT', f'{name} 형식을 확인해 주세요.')
    value = value.strip()
    require((optional or bool(value)) and len(value) <= maximum and not any(ord(c) < 32 for c in value),
            'INVALID_INPUT', f'{name} 길이와 내용을 확인해 주세요.')
    return value


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def expected_revision(value):
    require(type(value) is int and value > 0, 'INVALID_INPUT', '수정 버전을 확인해 주세요.')
    return value


def track_duration(value):
    require(type(value) is int and 1 <= value <= 2_147_483_647,
            'INVALID_DURATION', '유효한 곡 길이를 입력해 주세요.', 422)
    return value


def youtube_url(value):
    value = text(value, '영상 URL', 2048, optional=True)
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        require(parts.scheme == 'https' and not parts.username and not parts.password and not parts.port,
                'INVALID_URL', '허용된 HTTPS YouTube 주소를 입력해 주세요.')
        host = (parts.hostname or '').lower()
        if host == 'youtu.be':
            video_id = parts.path.strip('/')
        else:
            require(host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'},
                    'INVALID_URL', 'YouTube 주소를 입력해 주세요.')
            if parts.path == '/watch':
                ids = parse_qs(parts.query).get('v', [])
                require(len(ids) == 1, 'INVALID_URL', '영상 ID를 확인해 주세요.')
                video_id = ids[0]
            else:
                segments = parts.path.strip('/').split('/')
                require(len(segments) == 2 and segments[0] in {'shorts', 'embed'},
                        'INVALID_URL', '영상 주소를 확인해 주세요.')
                video_id = segments[1]
        require(re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id) is not None,
                'INVALID_URL', '영상 ID를 확인해 주세요.')
    except (ValueError, TypeError):
        require(False, 'INVALID_URL', '영상 주소를 확인해 주세요.')
    return f'https://www.youtube.com/watch?v={video_id}'


def song_inputs(data):
    shape(data, ['venue_id', 'items'], ['items'])
    require(type(data['items']) is list and 1 <= len(data['items']) <= 20,
            'INVALID_ITEMS', '한 번에 1~20곡을 입력해 주세요.')
    venue_id = data.get('venue_id')
    if venue_id is not None:
        venue_id = text(venue_id, '업체 ID', 128)
    rows = []
    exact_keys = set()
    for item in data['items']:
        shape(item, ['track_name', 'artist_name', 'youtube_url'], ['track_name', 'artist_name'])
        row = {'track_name': text(item['track_name'], '곡명'),
               'artist_name': text(item['artist_name'], '아티스트'),
               'youtube_url': youtube_url(item.get('youtube_url', ''))}
        row['normalized_key'] = fingerprint([normalized(row['track_name']), normalized(row['artist_name'])])
        row['exact_key'] = fingerprint([row['normalized_key'], row['youtube_url']])
        require(row['exact_key'] not in exact_keys, 'DUPLICATE_ITEMS', '같은 입력 곡이 묶음 안에 중복됐습니다.')
        exact_keys.add(row['exact_key'])
        rows.append(row)
    return venue_id, rows
