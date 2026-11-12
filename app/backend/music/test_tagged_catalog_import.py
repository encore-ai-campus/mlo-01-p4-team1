import hashlib
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, override_settings

from backend.music.models import TaggedMusicCandidate, Track
from backend.recommendation.catalog_storage import pack
from backend.recommendation.models import Catalog, Job
from backend.recommendation.service import current_catalog
from backend.recommendation.worker import run_one, verified_catalog


@override_settings(
    LOCAL_DEMO=True, INTEGRATION_ENABLED=True, YOUTUBE_REGISTRY_BACKEND='database',
    YOUTUBE_PLAYBACK_MODE='KEYLESS', MUSIC_S3_BUCKET='', MUSIC_S3_ARCHIVE_DEFERRED=False,
    OWNER_RESOLVER='backend.common.identity.owner', EVENT_SINK='backend.common.events.record',
    WEATHER_PROVIDER='', SEARCH_SECONDS=20, SEARCH_MAX_CANDIDATES=96, GUEST_TTL_SECONDS=600, GEMINI_ENABLED=False,
)
class TaggedCatalogImportTests(TestCase):
    recording = 'a' * 64
    video_id = 'Abc123DeFgH'

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        settings = override_settings(MUSIC_RAW_DIRECTORY=str(self.root / 'archive'))
        settings.enable()
        self.addCleanup(settings.disable)
        self.source = self.root / 'tagged.json'
        self.reviews = self.root / 'review.json'
        self.url = f'https://www.youtube.com/watch?v={self.video_id}'
        self.record = {
            'audio_sha256': self.recording, 'youtube_video_id': self.video_id,
            'youtube_url': self.url, 'title': '검수 음원', 'artists': ['테스트 연주자'],
            'album_name': '검수 앨범', 'duration_ms': 240_000, 'labels': [],
            'predictions': [{'genre_tags': ['ROCK_PUNK_METAL'], 'mood_tags': ['CALM'],
                             'review_status': 'NEEDS_REVIEW', 'automatic_use_enabled': False}],
        }
        self.review = {
            'audio_sha256': self.recording, 'genre_groups': ['ROCK'], 'theme_codes': ['CALM'],
            'explicit': False, 'reviewed_duration_ms': 240_000,
            'identity_and_embed_confirmed': True,
            'audio': {'energy': 0.6, 'acousticness': 0.4, 'valence': 0.7, 'mode': True, 'source': 'test'},
        }
        self.reviewer = get_user_model().objects.create_user(username='tagged-reviewer', is_superuser=True)

    def stage(self, rows, *, apply=True):
        raw = json.dumps({'schema_version': 'muther-tagged-tracks.v1', 'tracks': rows}).encode()
        self.source.write_bytes(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        call_command('import_tagged_music_catalog', str(self.source), expected_sha256=self.digest,
                     apply=apply, stdout=StringIO())

    def publish(self, reviews):
        self.reviews.write_text(json.dumps(reviews), encoding='utf-8')
        call_command('publish_tagged_music_catalog', str(self.reviews), source_sha256=self.digest,
                     catalog_version='reviewed-tags', reviewer=self.reviewer.username, apply=True, stdout=StringIO())

    def test_stage_retry_and_publish_into_playlist_with_iframe_mapping(self):
        self.stage([self.record], apply=False)
        self.assertFalse(TaggedMusicCandidate.objects.exists())
        self.stage([self.record])
        self.stage([self.record])
        self.assertEqual(TaggedMusicCandidate.objects.get().record, self.record)
        self.assertFalse(Track.objects.exists())

        call_command('seed_c_demo', stdout=StringIO())
        demo = Catalog.objects.get(pk='c-demo-v3')
        tracks, digest = pack(verified_catalog(demo)[:44])
        Catalog.objects.create(version='base-tags', tracks=tracks, sha256=digest, source_ref='fixture',
                               source_sha256='b' * 64, ready=True, is_test=True)
        self.publish([self.review])
        catalog = current_catalog()
        self.assertEqual(catalog.pk, 'reviewed-tags')
        track = TaggedMusicCandidate.objects.get().track
        rows = verified_catalog(catalog)
        self.assertEqual(len(rows), 45)
        row = next(row for row in rows if row['track_id'] == track.pk)
        self.assertEqual((row['genre_group_codes'], row['theme_codes'], row['play_url']),
                         (['ROCK'], ['CALM'], self.url))
        self.assertIsNone(row['speed'])

        guest = Client()
        for mode in ('OFF', 'ON'):
            with self.subTest(weather=mode):
                started = guest.post('/api/recommendation-jobs', {
                    'region_id': 'demo-region', 'weather_influence_mode': mode, 'selected_theme_codes': ['CALM'],
                }, content_type='application/json', HTTP_IDEMPOTENCY_KEY=f'tagged-catalog-test-{mode}')
                self.assertEqual(started.status_code, 202, started.content)
                job_id = started.json()['job_id']
                self.assertEqual(Job.objects.get(pk=job_id).catalog_id, catalog.pk)
                self.assertTrue(run_one(queue='rule'))
                job = guest.get(f'/api/recommendation-jobs/{job_id}').json()
                self.assertEqual(job['status'], 'SUCCEEDED', job)
                playlist_path = f"/api/playlists/{job['playlist_id']}"
                playlist = guest.get(playlist_path).json()
                item = next(item for item in playlist['items'] if item['track_id'] == track.pk)
                self.assertEqual(item['theme_codes'], ['CALM'])
                self.assertNotIn('speed', item)
                if mode == 'ON':
                    self.assertIsNotNone(item['scores']['W'])
                playback = guest.post(playlist_path + '/youtube-playback', {'track_id': track.pk},
                                      content_type='application/json')
                self.assertEqual(playback.status_code, 200, playback.content)
                self.assertEqual(playback.json()['status'], 'READY')
                self.assertEqual(playback.json()['candidates'][0]['video_id'], self.video_id)

    def test_rejects_duplicate_source_and_rolls_back_failed_publication(self):
        with self.assertRaisesRegex(CommandError, 'duplicate YouTube'):
            self.stage([self.record, {**self.record, 'audio_sha256': 'c' * 64}])
        self.assertFalse(TaggedMusicCandidate.objects.exists())
        self.stage([self.record])
        with self.assertRaises(CommandError):
            self.publish([{**self.review, 'reviewed_duration_ms': 600_000}])
        self.assertFalse(Track.objects.exists())
        self.assertFalse(Catalog.objects.exists())
        self.assertIsNone(TaggedMusicCandidate.objects.get().track_id)
