import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings

from backend.recommendation import youtube_audio as audio
from backend.recommendation import youtube_audio_worker as worker
from backend.recommendation.errors import DomainError
from backend.recommendation.models import Playlist, Job, PendingEvent
from backend.recommendation.worker import run_one
from backend.recommendation.youtube_playback import _keyless_candidates


VIDEO = 'testVID0001'
TRACK = {'track_id': 'song-1', 'track_name': 'Song', 'artists_name': 'Artist', 'duration_ms': 180000}


def ready(video_id=VIDEO):
    expires = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {'status': 'READY', 'video_id': video_id,
            'audio_url': f'https://r1.googlevideo.com/videoplayback?expire={int(expires.timestamp())}&signature=private-test-fixture',
            'thumbnail_url': f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg', 'expires_at': expires.isoformat()}


def mapping(**changes):
    return {'video_id': VIDEO, 'status': 'VERIFIED', 'identity_confirmed': True,
            'playback_observed': True, 'duration_seconds': 180, 'native_verified': False,
            'native_source': '', **changes}


class YoutubeAudioUnitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        with audio._lock:
            audio._sources.clear(); audio._inflight.clear()

    def test_request_accepts_ids_only_and_rejects_urls_extra_fields_and_wrong_types(self):
        self.assertEqual(audio.validate_audio_request({'track_id': 'song-1', 'video_id': VIDEO}), ('song-1', VIDEO))
        for body in ({'track_id': 'song-1', 'video_id': 'https://youtube.com/watch?v=' + VIDEO},
                     {'track_id': 'song-1', 'video_id': VIDEO, 'url': 'https://internal.example'},
                     {'track_id': True, 'video_id': VIDEO}, {'track_id': 'song-1'}, []):
            with self.subTest(body=body), self.assertRaises(DomainError):
                audio.validate_audio_request(body)

    def test_audio_requires_reviewed_identity_or_native_confirmation_and_honors_revocation(self):
        rows = [mapping(), mapping(status='CANDIDATE', identity_confirmed=False, playback_observed=False),
                mapping(status='BLOCKED', native_verified=True, native_source='OPERATOR_NATIVE_REPORTED_PLAYING'),
                mapping(native_verified=True, native_source='OPERATOR_NATIVE_BLOCK')]
        for row, allowed in zip(rows, (True, False, True, False)):
            with self.subTest(row=row), patch.object(audio.youtube_registry, 'track_registry', return_value={'mappings': [row]}):
                self.assertEqual(audio.authorized_video(TRACK, VIDEO), allowed)
        catalog = {**TRACK, 'youtube_video_id': VIDEO, 'youtube_match_status': 'VERIFIED'}
        with patch.object(audio.youtube_registry, 'track_registry', return_value={'mappings': []}):
            self.assertTrue(audio.authorized_video(catalog, VIDEO))
        with patch.object(audio.youtube_registry, 'track_registry', return_value={'mappings': [mapping(native_source='OPERATOR_NATIVE_BLOCK')]}):
            self.assertFalse(audio.authorized_video(catalog, VIDEO))

    @override_settings(LOCAL_DEMO=True)
    def test_keyless_native_hint_does_not_promote_blocked_iframe_mapping(self):
        row = mapping(status='BLOCKED', native_verified=True, native_source='OPERATOR_NATIVE_REPORTED_PLAYING')
        original = dict(row)
        with patch('backend.recommendation.youtube_playback.youtube_registry.track_registry', return_value={'revision': 2, 'mappings': [row]}):
            result = _keyless_candidates(TRACK, [])
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['candidates'][0]['playback_hint'], 'AUDIO')
        self.assertEqual(row, original)
        for row in (mapping(status='CANDIDATE', identity_confirmed=False, playback_observed=False),
                    mapping(status='BLOCKED', native_verified=True, native_source='OPERATOR_NATIVE_BLOCK')):
            with patch('backend.recommendation.youtube_playback.youtube_registry.track_registry', return_value={'revision': 2, 'mappings': [row]}):
                self.assertEqual(_keyless_candidates(TRACK, [])['status'], 'MAPPING_REQUIRED')

    @override_settings(LOCAL_DEMO=False, YOUTUBE_REGISTRY_BACKEND='disabled')
    def test_verified_catalog_works_when_no_registry_was_configured(self):
        catalog = {**TRACK, 'youtube_video_id': VIDEO, 'youtube_match_status': 'VERIFIED'}
        with patch.object(audio.youtube_registry, 'track_registry', side_effect=AssertionError('unconfigured registry')):
            self.assertTrue(audio.authorized_video(catalog, VIDEO))
            self.assertFalse(audio.authorized_video(TRACK, VIDEO))
        with override_settings(YOUTUBE_REGISTRY_BACKEND='database'), patch.object(audio.youtube_registry, 'track_registry', side_effect=DomainError('REGISTRY_UNAVAILABLE', 'offline', 503)):
            with self.assertRaises(DomainError):
                audio.authorized_video(catalog, VIDEO)

    def test_worker_uses_only_matching_non_drm_audio_https_host_and_expiring_url(self):
        fmt = {'has_drm': False, 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a',
               'protocol': 'https', 'url': 'https://r1.googlevideo.com/videoplayback?expire=2000'}
        info = {'id': VIDEO, 'formats': [fmt], 'thumbnail': 'https://foreign.example/cover.jpg'}
        result = worker.source_payload(info, VIDEO, now=1000)
        self.assertEqual(result['status'], 'READY')
        self.assertEqual(result['thumbnail_url'], f'https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg')
        for change in ({'has_drm': True}, {'has_drm': None}, {'vcodec': 'avc1'},
                       {'url': 'https://r1.googlevideo.com.evil.example/videoplayback?expire=2000'},
                       {'url': 'http://r1.googlevideo.com/videoplayback?expire=2000'},
                       {'url': 'https://r1.googlevideo.com/videoplayback?expire=1001'}):
            with self.subTest(change=change):
                self.assertEqual(worker.source_payload({**info, 'formats': [{**fmt, **change}]}, VIDEO, now=1000)['status'], 'UNAVAILABLE')
        self.assertEqual(worker.source_payload({**info, 'id': 'otherVID001'}, VIDEO, now=1000)['reason'], 'INVALID_SOURCE')
        self.assertEqual(worker.source_payload({**info, 'age_limit': 18}, VIDEO, now=1000)['reason'], 'ACCESS_RESTRICTED')

    def test_worker_failure_never_returns_signed_url_or_exception_text(self):
        payload = worker.unavailable(worker.classify_failure('HTTP 429 https://r1.googlevideo.com/videoplayback?signature=secret'))
        self.assertEqual(payload['reason'], 'RATE_LIMITED')
        self.assertNotIn('googlevideo', json.dumps(payload))
        self.assertEqual(worker.classify_failure('Sign in to confirm you are not a bot'), 'ACCESS_RESTRICTED')

    def test_subprocess_timeout_and_output_validation_never_expose_stderr(self):
        with patch.object(audio.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 20, stderr='signature=secret')):
            self.assertEqual(audio._worker(VIDEO)['reason'], 'TIMEOUT')
        for payload in ({**ready(), 'audio_url': 'https://evil.example/media'},
                        {**ready(), 'video_id': 'otherVID001'}, {**ready(), 'thumbnail_url': 'http://127.0.0.1/private'}):
            result = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='signature=secret')
            with patch.object(audio.subprocess, 'run', return_value=result) as run:
                response = audio._worker(VIDEO)
            self.assertEqual(response['reason'], 'INVALID_SOURCE')
            self.assertNotIn('secret', json.dumps(response))
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
            self.assertIn('-I', run.call_args.args[0])

    def test_success_is_memory_cached_bounded_and_permission_is_rechecked_before_cache_hit(self):
        source = ready()
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker', return_value=source) as extract:
            self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner'), source)
            self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner'), source)
            extract.assert_called_once()
        with patch.object(audio, 'authorized_video', return_value=False), patch.object(audio, '_worker') as extract:
            with self.assertRaises(DomainError):
                audio.resolve_audio(TRACK, VIDEO, 'owner')
            extract.assert_not_called()
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker', side_effect=lambda value: ready(value)), patch.object(audio, 'MAX_CACHED_SOURCES', 2):
            for value in ('testVID0002', 'testVID0003', 'testVID0004'):
                audio.resolve_audio(TRACK, value, 'owner')
        self.assertEqual(len(audio._sources), 2)
        self.assertNotIn(VIDEO, audio._sources)

    def test_duplicate_or_busy_extraction_does_not_start_another_worker(self):
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker') as extract:
            with audio._lock:
                audio._inflight.add(VIDEO)
            self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner')['reason'], 'BUSY')
            extract.assert_not_called()

    def test_live_cache_ttl_does_not_reuse_a_source_with_less_than_ninety_seconds_left(self):
        cached = ready()
        cached['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=89)).isoformat()
        fresh = ready()
        with audio._lock:
            audio._sources[VIDEO] = (audio.time.monotonic() + 60, cached)
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker', return_value=fresh) as extract:
            self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner'), fresh)
        extract.assert_called_once_with(VIDEO)
        self.assertEqual(audio._sources[VIDEO][1], fresh)

    def test_cached_source_with_sufficient_remaining_lifetime_does_not_reextract(self):
        cached = ready()
        cached['expires_at'] = (datetime.now(timezone.utc) + timedelta(seconds=150)).isoformat()
        with audio._lock:
            audio._sources[VIDEO] = (audio.time.monotonic() + 60, cached)
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker') as extract:
            self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner'), cached)
        extract.assert_not_called()

    def test_two_running_workers_are_a_hard_process_limit(self):
        self.assertTrue(audio._slots.acquire(blocking=False))
        self.assertTrue(audio._slots.acquire(blocking=False))
        try:
            with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker') as extract:
                self.assertEqual(audio.resolve_audio(TRACK, VIDEO, 'owner')['reason'], 'BUSY')
                extract.assert_not_called()
        finally:
            audio._slots.release(); audio._slots.release()
        self.assertNotIn(VIDEO, audio._inflight)

    def test_one_owner_cannot_start_an_unbounded_number_of_extractors(self):
        with patch.object(audio, 'authorized_video', return_value=True), patch.object(audio, '_worker', side_effect=lambda value: ready(value)) as extract:
            for index in range(12):
                self.assertEqual(audio.resolve_audio(TRACK, f'testVID{index:04}', 'owner')['status'], 'READY')
            self.assertEqual(audio.resolve_audio(TRACK, 'testVID9999', 'owner')['reason'], 'RATE_LIMITED')
        self.assertEqual(extract.call_count, 12)


@override_settings(LOCAL_DEMO=True, WEATHER_PROVIDER='', SEARCH_SECONDS=20, SEARCH_MAX_CANDIDATES=96,
                   YOUTUBE_DATA_API_KEY='', YOUTUBE_PLAYBACK_MODE='OPTIONAL_SEARCH')
class YoutubeAudioApiTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.registry = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(YOUTUBE_REGISTRY_PATH=Path(self.registry.name) / 'mappings.sqlite3')
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.registry.cleanup)
        call_command('seed_c_demo', verbosity=0)
        self.client = Client()
        self.client.get('/api/c-demo/context')
        response = self.client.post('/api/recommendation-jobs', {'region_id': 'demo-region'},
                                    content_type='application/json', HTTP_IDEMPOTENCY_KEY='audio-fixture')
        self.assertEqual(response.status_code, 202, response.content)
        self.assertTrue(run_one())
        self.playlist = Playlist.objects.get(job_id=response.json()['job_id'])
        self.track = self.playlist.snapshot['items'][0]
        self.path = f'/api/playlists/{self.playlist.pk}/youtube-audio'

    def post(self, body=None, client=None):
        return (client or self.client).post(self.path, body or {'track_id': self.track['track_id'], 'video_id': VIDEO}, content_type='application/json')

    def test_authorized_response_is_no_store_and_does_not_mutate_playlist_or_mappings(self):
        before = self.playlist.snapshot
        jobs = Job.objects.count()
        with patch('backend.recommendation.youtube_audio_api.resolve_audio', return_value=ready()) as resolve:
            response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response.json()['status'], 'READY')
        resolve.assert_called_once()
        self.playlist.refresh_from_db()
        self.assertEqual(self.playlist.snapshot, before)
        self.assertEqual(Job.objects.count(), jobs)
        self.assertFalse(PendingEvent.objects.exists())

    def test_foreign_owner_outside_track_and_unapplied_exclusion_never_reach_audio(self):
        foreign = Client(); foreign.get('/api/c-demo/context')
        with patch('backend.recommendation.youtube_audio_api.resolve_audio') as resolve:
            self.assertEqual(self.post(client=foreign).status_code, 404)
            self.assertEqual(self.post({'track_id': 'outside', 'video_id': VIDEO}).status_code, 404)
            family = self.playlist.family
            family.excluded = [self.track['track_id']]
            family.save(update_fields=['excluded'])
            self.assertEqual(self.post().status_code, 409)
            resolve.assert_not_called()

    def test_invalid_body_and_missing_csrf_never_reach_audio(self):
        strict = Client(enforce_csrf_checks=True)
        strict.cookies = self.client.cookies
        with patch('backend.recommendation.youtube_audio_api.resolve_audio') as resolve:
            self.assertEqual(self.post({'track_id': self.track['track_id'], 'video_id': VIDEO, 'url': 'https://foreign.example'}).status_code, 422)
            self.assertEqual(self.post(client=strict).status_code, 403)
            resolve.assert_not_called()

    def test_arbitrary_video_is_rejected_before_worker(self):
        with patch('backend.recommendation.youtube_audio.youtube_registry.track_registry', return_value={'mappings': []}), patch.object(audio, '_worker') as extract:
            response = self.post()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'AUDIO_MAPPING_REQUIRED')
        self.assertEqual(response['Cache-Control'], 'no-store')
        extract.assert_not_called()
