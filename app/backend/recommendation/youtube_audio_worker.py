"""Isolated metadata-only extraction; stdout is an in-memory IPC response.

Never save media, cookies, a downloader cache, metadata JSON, or signed URLs.
The parent imposes a hard timeout and ignores stderr rather than logging it.
"""
import contextlib
import io
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit


VERSION = '2026.10.26'
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
REASONS = {'UNAVAILABLE', 'ACCESS_RESTRICTED', 'DRM_UNSUPPORTED', 'INVALID_SOURCE',
           'TIMEOUT', 'RATE_LIMITED', 'NETWORK_ERROR', 'EXTRACTOR_UNAVAILABLE'}


def unavailable(reason):
    return {'status': 'UNAVAILABLE', 'reason': reason if reason in REASONS else 'UNAVAILABLE'}


def classify_failure(error):
    value = str(error).casefold()
    if any(term in value for term in ('429', 'too many requests', 'rate limit')):
        return 'RATE_LIMITED'
    if any(term in value for term in ('not a bot', 'captcha', 'sign in', 'log in', 'login',
                                      'age-restricted', 'confirm your age', 'country', 'region',
                                      'members-only', 'premium-only')):
        return 'ACCESS_RESTRICTED'
    if 'drm' in value:
        return 'DRM_UNSUPPORTED'
    if any(term in value for term in ('timed out', 'timeout')):
        return 'TIMEOUT'
    if any(term in value for term in ('unavailable', 'not available', 'private video', 'removed', 'deleted')):
        return 'UNAVAILABLE'
    return 'NETWORK_ERROR'


def trusted_url(value, hosts, subdomains=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 16384 or '\\' in value:
        return False
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        allowed = host in hosts or (subdomains and any(host.endswith('.' + domain) for domain in hosts))
        return (parsed.scheme == 'https' and allowed and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


def source_payload(info, video_id, now=None):
    now = time.time() if now is None else now
    if not isinstance(info, dict) or info.get('id') != video_id:
        return unavailable('INVALID_SOURCE')
    if info.get('age_limit', 0) or info.get('is_live') or info.get('availability') in {
        'private', 'needs_auth', 'premium_only', 'subscriber_only', 'needs_subscription', 'is_upcoming',
    }:
        return unavailable('ACCESS_RESTRICTED')
    choices = []
    for item in info.get('formats') or []:
        if not isinstance(item, dict) or item.get('has_drm') is not False:
            continue
        if item.get('vcodec') != 'none' or item.get('acodec') in (None, 'none'):
            continue
        if item.get('ext') not in ('m4a', 'webm') or item.get('protocol') != 'https':
            continue
        if not trusted_url(item.get('url'), {'googlevideo.com'}, subdomains=True):
            continue
        try:
            expiry = parse_qs(urlsplit(item['url']).query).get('expire', [])
            if len(expiry) != 1:
                continue
            expiry = float(expiry[0])
            if not math.isfinite(expiry) or expiry < now + 120:
                continue
        except (ValueError, TypeError):
            continue
        # M4A is preferred for broad browser support; WebM is a fallback.
        bitrate = item.get('abr') or item.get('tbr') or 0
        bitrate = float(bitrate) if isinstance(bitrate, (int, float)) and math.isfinite(bitrate) else 0
        choices.append((item.get('ext') == 'm4a', bitrate, item, expiry))
    if not choices:
        return unavailable('DRM_UNSUPPORTED' if any(isinstance(item, dict) and item.get('has_drm') for item in info.get('formats') or []) else 'UNAVAILABLE')
    _, _, selected, expiry = max(choices, key=lambda item: item[:2])
    thumbnail = info.get('thumbnail')
    if not trusted_url(thumbnail, {'i.ytimg.com', 'img.youtube.com'}):
        thumbnail = f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'
    return {'status': 'READY', 'video_id': video_id, 'audio_url': selected['url'],
            'thumbnail_url': thumbnail,
            'expires_at': datetime.fromtimestamp(min(expiry, now + 300), timezone.utc).isoformat()}


class QuietLogger:
    def debug(self, *_args, **_kwargs): pass
    def info(self, *_args, **_kwargs): pass
    def warning(self, *_args, **_kwargs): pass
    def error(self, *_args, **_kwargs): pass


def extract(video_id):
    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
        return unavailable('INVALID_SOURCE')
    try:
        import yt_dlp
        from yt_dlp.version import __version__
    except ImportError:
        return unavailable('EXTRACTOR_UNAVAILABLE')
    if __version__ != VERSION:
        return unavailable('EXTRACTOR_UNAVAILABLE')
    options = {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
               'skip_download': True, 'noplaylist': True, 'cachedir': False,
               'socket_timeout': 5, 'retries': 0, 'extractor_retries': 0,
               'fragment_retries': 0, 'geo_bypass': False,
               'allow_unplayable_formats': False, 'age_limit': 0,
               'writeinfojson': False, 'writethumbnail': False,
               'writesubtitles': False, 'writeautomaticsub': False,
               'usenetrc': False, 'cookiefile': None, 'cookiesfrombrowser': None}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=False)
        return source_payload(info, video_id)
    except Exception as error:
        # Never return an exception's text: it can contain a signed media URL.
        return unavailable(classify_failure(error))


if __name__ == '__main__':
    result = extract(sys.argv[1]) if len(sys.argv) == 2 else unavailable('INVALID_SOURCE')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
