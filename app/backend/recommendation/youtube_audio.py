"""Authorized direct-audio metadata in bounded, process-local memory only."""
import hashlib
import json
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from . import youtube_registry
from .errors import require
from .youtube_audio_worker import VIDEO_ID, REASONS, trusted_url


_lock = threading.Lock()
_slots = threading.BoundedSemaphore(2)
_sources = OrderedDict()
_inflight = set()
MAX_CACHED_SOURCES = 64
SOURCE_MEMORY_SECONDS = 60
MESSAGES = {
    'UNAVAILABLE': '이 영상에서 재생 가능한 오디오를 확인하지 못했어요.',
    'ACCESS_RESTRICTED': '로그인·지역·연령 등의 접근 제한이 있어요.',
    'DRM_UNSUPPORTED': '이 영상에는 사용할 수 있는 비DRM 오디오가 없어요.',
    'INVALID_SOURCE': '오디오 출처를 확인하지 못했어요.',
    'TIMEOUT': '오디오 준비 시간이 초과됐어요. 잠시 후 다시 확인해 주세요.',
    'RATE_LIMITED': '오디오 준비 요청이 많아요. 잠시 후 다시 확인해 주세요.',
    'NETWORK_ERROR': '오디오 출처에 연결하지 못했어요.',
    'EXTRACTOR_UNAVAILABLE': '오디오 연결 도구의 설치 버전을 확인해야 해요.',
    'BUSY': '오디오를 준비 중이에요. 잠시 후 다시 확인해 주세요.',
}


def failure(video_id, reason):
    reason = reason if reason in MESSAGES else 'UNAVAILABLE'
    return {'status': 'UNAVAILABLE', 'video_id': video_id, 'reason': reason, 'message': MESSAGES[reason]}


def validate_audio_request(body):
    require(isinstance(body, dict) and set(body) == {'track_id', 'video_id'})
    track_id, video_id = body['track_id'], body['video_id']
    require(isinstance(track_id, str) and bool(track_id.strip()) and len(track_id) <= 256)
    require(isinstance(video_id, str) and VIDEO_ID.fullmatch(video_id))
    return track_id, video_id


def authorized_video(track, video_id):
    catalog_verified = track.get('youtube_match_status') == 'VERIFIED' and track.get('youtube_video_id') == video_id
    registry_configured = settings.LOCAL_DEMO or getattr(settings, 'YOUTUBE_REGISTRY_BACKEND', 'disabled') == 'database'
    if not registry_configured:
        # The immutable verified catalog remains an authority where the mutable
        # registry was never configured. A configured but failing registry does
        # not fall back: that could bypass a revocation we cannot currently read.
        return catalog_verified
    rows = [row for row in youtube_registry.track_registry(track['track_id'])['mappings']
            if row.get('video_id') == video_id]
    if any(row.get('native_source') == 'OPERATOR_NATIVE_BLOCK' for row in rows):
        return False
    native_confirmed = any(row.get('native_verified') and row.get('native_source') == 'OPERATOR_NATIVE_REPORTED_PLAYING' for row in rows)
    if any(row.get('status') == 'BLOCKED' for row in rows) and not native_confirmed:
        return False
    if catalog_verified:
        return True
    for row in rows:
        iframe_verified = row.get('status') == 'VERIFIED' and row.get('identity_confirmed') and row.get('playback_observed')
        native_verified = row.get('native_verified') and row.get('native_source') == 'OPERATOR_NATIVE_REPORTED_PLAYING'
        if iframe_verified or native_verified:
            return True
    return False


def _reserve_owner(owner_id):
    # Shared cache contains only counters, never a signed URL or metadata.
    owner_hash = hashlib.sha256(str(owner_id).encode()).hexdigest()[:24]
    key = f'yta:minute:{owner_hash}:{int(time.time() // 60)}'
    cache.add(key, 0, timeout=120)
    return cache.incr(key) <= 12


def _worker(video_id):
    # -I excludes user paths and PYTHONPATH; no shell or client-controlled URL.
    command = [sys.executable, '-I', '-B', str(Path(__file__).with_name('youtube_audio_worker.py')), video_id]
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return failure(video_id, 'TIMEOUT')
    except (OSError, ValueError):
        return failure(video_id, 'EXTRACTOR_UNAVAILABLE')
    if result.returncode != 0 or len(result.stdout) > 65536:
        return failure(video_id, 'EXTRACTOR_UNAVAILABLE')
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError):
        return failure(video_id, 'INVALID_SOURCE')
    if not isinstance(payload, dict):
        return failure(video_id, 'INVALID_SOURCE')
    if payload.get('status') != 'READY':
        return failure(video_id, payload.get('reason') if payload.get('reason') in REASONS else 'UNAVAILABLE')
    if payload.get('video_id') != video_id or not trusted_url(payload.get('audio_url'), {'googlevideo.com'}, subdomains=True) or not trusted_url(payload.get('thumbnail_url'), {'i.ytimg.com', 'img.youtube.com'}):
        return failure(video_id, 'INVALID_SOURCE')
    try:
        expiry = datetime.fromisoformat(payload['expires_at'])
        seconds_left = (expiry - datetime.now(timezone.utc)).total_seconds()
        if expiry.tzinfo is None or not 60 <= seconds_left <= 301:
            return failure(video_id, 'INVALID_SOURCE')
    except (KeyError, ValueError, TypeError):
        return failure(video_id, 'INVALID_SOURCE')
    # Return explicit fields only; worker stderr, arbitrary metadata and errors
    # never propagate into response bodies, exceptions or application logs.
    return {key: payload[key] for key in ('status', 'video_id', 'audio_url', 'thumbnail_url', 'expires_at')}


def resolve_audio(track, video_id, owner_id):
    require(authorized_video(track, video_id), 'AUDIO_MAPPING_REQUIRED', '이 곡으로 확인된 영상만 오디오를 준비할 수 있어요.', 409)
    now = time.monotonic()
    with _lock:
        for key in list(_sources):
            if _sources[key][0] <= now:
                del _sources[key]
        if video_id in _sources:
            payload = _sources[video_id][1]
            try:
                expires = datetime.fromisoformat(payload['expires_at'])
                usable = (expires - datetime.now(timezone.utc)).total_seconds() >= 90
            except (KeyError, ValueError, TypeError):
                usable = False
            if usable:
                _sources.move_to_end(video_id)
                return dict(payload)
            # The local cache TTL is not proof that the source URL remains
            # useful for next-song preparation. Re-extract near-expiry URLs.
            del _sources[video_id]
        if video_id in _inflight:
            return failure(video_id, 'BUSY')
        _inflight.add(video_id)
    acquired = False
    try:
        if not _reserve_owner(owner_id):
            return failure(video_id, 'RATE_LIMITED')
        acquired = _slots.acquire(blocking=False)
        if not acquired:
            return failure(video_id, 'BUSY')
        payload = _worker(video_id)
        if payload['status'] == 'READY':
            with _lock:
                _sources[video_id] = (time.monotonic() + SOURCE_MEMORY_SECONDS, dict(payload))
                while len(_sources) > MAX_CACHED_SOURCES:
                    _sources.popitem(last=False)
        return payload
    finally:
        if acquired:
            _slots.release()
        with _lock:
            _inflight.discard(video_id)
