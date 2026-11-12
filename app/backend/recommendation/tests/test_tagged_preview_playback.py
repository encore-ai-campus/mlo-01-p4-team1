from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from backend.recommendation import youtube_audio, youtube_registry
from backend.recommendation.api import Endpoint
from backend.recommendation.errors import DomainError
from backend.recommendation.models import Catalog, Family, Job, Playlist


VIDEO = 'testVID0001'
OTHER_VIDEO = 'testVID0002'
TRACK_ID = 'tagged:' + 'a' * 64
SOURCE = 'local-preview:tagged-json:' + 'b' * 64
OWNER = 'tagged-preview-owner'


@override_settings(LOCAL_DEMO=True, INTEGRATION_ENABLED=False, OWNER_RESOLVER='',
                   YOUTUBE_PLAYBACK_MODE='KEYLESS', YOUTUBE_REGISTRY_BACKEND='local')
class TaggedPreviewPlaybackTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        catalog = Catalog.objects.create(version='tagged-preview', tracks=[], sha256='c' * 64,
                                         source_ref=SOURCE, source_sha256='b' * 64, ready=True, is_test=True)
        cls.family = Family.objects.create(owner_id=OWNER, options={}, policy_version='test', mapping_version='test')
        job = Job.objects.create(family=cls.family, catalog=catalog, input={}, request_id='fixture', status='SUCCEEDED')
        cls.playlist = Playlist.objects.create(job=job, family=cls.family, snapshot={
            'context': {'excluded_track_ids': []}, 'is_test': True, 'preview_only': True,
            'items': [{'track_id': TRACK_ID, 'track_name': '시험 곡', 'artists_name': '시험 가수',
                       'duration_ms': 180000, 'youtube_video_id': VIDEO,
                       'play_url': f'https://www.youtube.com/watch?v={VIDEO}'}],
        })

    def setUp(self):
        session = self.client.session
        session['c_demo_owner'] = OWNER
        session.save()
        self.registry = self.enterContext(patch.object(youtube_registry, 'track_registry',
                                                       return_value={'revision': 0, 'mappings': []}))
        self.enterContext(patch.dict(youtube_audio._sources, clear=True))
        self.enterContext(patch.object(youtube_audio, '_reserve_owner', return_value=True))
        self.worker = self.enterContext(patch.object(youtube_audio, '_worker', return_value={
            'status': 'READY', 'video_id': VIDEO, 'audio_url': 'https://example.googlevideo.com/sample',
            'thumbnail_url': f'https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg',
            'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        }))

    def post(self, route, body, client=None):
        return (client or self.client).post(f'/api/playlists/{self.playlist.pk}/{route}', body,
                                            content_type='application/json')

    def playback(self, **body):
        return self.post('youtube-playback', {'track_id': TRACK_ID, **body})

    def audio(self, **body):
        return self.post('youtube-audio', {'track_id': TRACK_ID, 'video_id': VIDEO, **body})

    def test_owned_unmapped_preview_offers_original_video_and_temporary_audio(self):
        before = self.playlist.snapshot
        playback = self.playback()
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.json(), {'track_id': TRACK_ID, 'status': 'READY', 'mapping_revision': 0,
                                          'candidates': [{'video_id': VIDEO, 'title': '시험 곡',
                                                          'channel_title': '시험 가수', 'duration_seconds': 180.0,
                                                          'source': 'TAGGED_JSON_PREVIEW',
                                                          'match_status': 'CANDIDATE'}]})
        result = self.audio()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result['Cache-Control'], 'no-store')
        self.assertEqual(result.json()['video_id'], VIDEO)
        self.worker.assert_called_once_with(VIDEO)
        self.playlist.refresh_from_db()
        self.assertEqual(self.playlist.snapshot, before)

    def test_denies_foreign_owner_outside_song_and_unapplied_exclusions(self):
        for route, body in (('youtube-playback', {}), ('youtube-audio', {'video_id': VIDEO})):
            self.assertEqual(self.post(route, {'track_id': TRACK_ID, **body}, Client()).status_code, 404)
            self.assertEqual(self.post(route, {'track_id': 'outside', **body}).status_code, 404)
        self.family.excluded = [TRACK_ID]
        self.family.save(update_fields=['excluded'])
        self.assertEqual(self.playback().status_code, 409)
        self.assertEqual(self.audio().status_code, 409)
        self.registry.assert_not_called()
        self.worker.assert_not_called()

    def test_denies_wrong_video_excluded_video_and_mismatched_canonical_url(self):
        self.assertEqual(self.audio(video_id=OTHER_VIDEO).status_code, 409)
        self.assertEqual(self.playback(excluded_video_ids=[VIDEO]).json()['status'], 'MAPPING_REQUIRED')
        self.playlist.snapshot['items'][0]['play_url'] = f'https://www.youtube.com/watch?v={OTHER_VIDEO}'
        self.playlist.save(update_fields=['snapshot'])
        self.assertEqual(self.playback().json()['status'], 'MAPPING_REQUIRED')
        self.assertEqual(self.audio().status_code, 409)
        self.worker.assert_not_called()

    def test_existing_approval_wins_and_blocked_pending_or_unavailable_registry_stay_closed(self):
        approved = {'video_id': OTHER_VIDEO, 'status': 'VERIFIED', 'identity_confirmed': True,
                    'playback_observed': True, 'duration_seconds': 180}
        self.registry.return_value = {'revision': 1, 'mappings': [approved]}
        candidate = self.playback().json()['candidates'][0]
        self.assertEqual((candidate['video_id'], candidate['match_status']), (OTHER_VIDEO, 'VERIFIED'))
        self.assertEqual(self.audio().status_code, 409)
        for status, reason in (('CANDIDATE', 'PENDING'), ('BLOCKED', 'BLOCKED')):
            with self.subTest(status=status):
                self.registry.return_value = {'revision': 2, 'mappings': [{**approved, 'video_id': VIDEO,
                                                                           'status': status}]}
                self.assertEqual(self.playback().json()['reason'], reason)
                self.assertEqual(self.audio().status_code, 409)
        self.registry.side_effect = DomainError('REGISTRY_UNAVAILABLE', 'unavailable', 503)
        self.assertEqual(self.playback().json()['reason'], 'REGISTRY_UNAVAILABLE')
        self.assertEqual(self.audio().status_code, 503)
        self.worker.assert_not_called()

    def test_other_test_catalog_and_operating_mode_never_receive_preview_access(self):
        catalog = self.playlist.job.catalog
        with override_settings(LOCAL_DEMO=False, YOUTUBE_REGISTRY_BACKEND='database'), \
                patch.object(Endpoint, 'owner', return_value=OWNER):
            self.assertEqual(self.playback().json()['status'], 'MAPPING_REQUIRED')
            self.assertEqual(self.audio().status_code, 409)
        with override_settings(INTEGRATION_ENABLED=True):
            self.assertEqual(self.playback().json()['status'], 'MAPPING_REQUIRED')
            self.assertEqual(self.audio().status_code, 409)
        catalog.source_ref = 'local-preview:other-source'
        catalog.save(update_fields=['source_ref'])
        self.assertEqual(self.playback().json()['status'], 'MAPPING_REQUIRED')
        self.assertEqual(self.audio().status_code, 409)
        self.worker.assert_not_called()
