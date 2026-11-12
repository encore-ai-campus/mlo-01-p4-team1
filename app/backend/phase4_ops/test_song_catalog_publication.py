from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from backend.common.models import MusicSource, Review, TrackOrigin
from backend.music.catalog import publish
from backend.music.models import Album, Tag, Track, TrackTag
from backend.recommendation import youtube_registry
from backend.recommendation.catalog_storage import pack
from backend.recommendation.errors import DomainError
from backend.recommendation.models import Catalog, Job
from backend.recommendation.service import current_catalog
from backend.recommendation.worker import run_one, verified_catalog
from backend.workspaces.models import BusinessEntity, Workspace

from .access import Principal
from .models import History, InferenceReceipt, PipelineTask, SongItem, SongSubmission
from .pipeline_services import claim, receive
from .registration_services import finalize
from .song_services import review
from .validation import fingerprint


VIDEO_ID = 'ABCDEFGHIJK'
VIDEO_URL = f'https://www.youtube.com/watch?v={VIDEO_ID}'
SOURCE_SHA = 'a' * 64
SOURCE_REF = f'music-source:{SOURCE_SHA}'


@override_settings(
    LOCAL_DEMO=False,
    INTEGRATION_ENABLED=True,
    ADMIN_PASSKEY_REQUIRED=True,
    GUEST_TTL_SECONDS=600,
    OWNER_RESOLVER='backend.common.identity.owner',
    EVENT_SINK='backend.common.events.record',
    SEARCH_SECONDS=20,
    SEARCH_MAX_CANDIDATES=96,
    GEMINI_ENABLED=False,
    YOUTUBE_REGISTRY_BACKEND='database',
    YOUTUBE_PLAYBACK_MODE='KEYLESS',
)
class SongCatalogPublicationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.admin = users.create_user(username='catalog-reviewer', is_staff=True)
        cls.admin.groups.add(Group.objects.get_or_create(name='OPS_ADMIN')[0])
        cls.ads_admin = users.create_user(username='catalog-ads-reviewer', is_staff=True)
        cls.ads_admin.groups.add(Group.objects.get_or_create(name='ADS_ADMIN')[0])
        owner = users.create_user(username='catalog-song-owner')
        business = BusinessEntity.objects.create(
            registration_lookup='catalog-song-business', registration_ciphertext='', display_name='테스트 업체',
        )
        workspace = Workspace.objects.create(
            business=business, location_key='catalog-song-workspace', display_name='테스트 매장',
            address_ciphertext='', status='ACTIVE',
        )
        submission = SongSubmission.objects.create(account_id=str(owner.pk), quota_key='catalog-song', venue=workspace)
        cls.item = SongItem.objects.create(
            submission=submission, position=1, track_name='신규 승인곡', artist_name='테스트 연주자',
            youtube_url=VIDEO_URL, normalized_key='catalog-song-normalized', exact_key='catalog-song-exact',
        )
        operator = Principal(str(cls.admin.pk), frozenset({'review_songs'}))
        review(operator, cls.item.pk, {
            'decision': 'APPROVE', 'expected_revision': cls.item.revision, 'youtube_url': VIDEO_URL,
        })
        cls.task = PipelineTask.objects.get(item=cls.item)
        claimed, _ = claim(cls.task.pk)
        tags = {'genre': ['JAZZ'], 'mood': ['CALM']}
        candidate = {
            'schema_version': 'phase4-inference-v1', 'result_id': 'catalog-song-result',
            'batch_id': 'catalog-song-batch', 'model_version': 'test-mert-ensemble-v1',
            'generated_at': timezone.now().isoformat(), 'tags': tags,
            'metadata': {'duration_ms': 240_000, 'album_name': '테스트 앨범', 'explicit': False},
            **{key: claimed[key] for key in ('task_id', 'request_item_id', 'lease_token', 'track_id', 'source_ref')},
        }
        receive({**candidate, 'payload_sha256': fingerprint(candidate)})
        cls.task.refresh_from_db()
        finalize(operator, cls.task.pk, {
            'expected_revision': cls.task.revision, 'tags': tags, 'duration_ms': 240_000,
            'album_name': '테스트 앨범', 'explicit': False,
        })
        cls.task.refresh_from_db()
        cls.item.refresh_from_db()

        with override_settings(LOCAL_DEMO=True):
            call_command('seed_c_demo', stdout=StringIO())
        demo = Catalog.objects.get(pk='c-demo-v3')
        tracks, digest = pack(verified_catalog(demo)[:44])
        cls.base = Catalog.objects.create(
            version='approved-song-base', tracks=tracks, sha256=digest,
            source_ref=SOURCE_REF, source_sha256=SOURCE_SHA, ready=True, is_test=False,
        )

    def setUp(self):
        self.admin_client = self.client_for(self.admin)
        self.path = f'/api/phase4/admin/tasks/{self.task.pk}/publish'
        self.body = {'expected_revision': self.task.revision, 'genre_groups': ['JAZZ']}

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        session = client.session
        session['admin_passkey_user'] = str(user.pk)
        session.save()
        return client

    def post(self, body=None, *, client=None, key=None):
        return (client or self.admin_client).post(
            self.path, self.body if body is None else body, content_type='application/json',
            HTTP_IDEMPOTENCY_KEY=key or str(uuid4()),
        )

    def publish_song(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        return Catalog.objects.get(pk=response.json()['registration']['catalog_version'])

    def test_existing_unqualified_row_is_replaced_in_new_version_only(self):
        rows = verified_catalog(self.base)
        legacy_row = {**rows[0], 'track_id': self.item.track_id, 'play_url': VIDEO_URL,
                      'speed': None, 'genre_group_codes': []}
        stored, digest = pack([*rows, legacy_row])
        legacy = Catalog.objects.create(version='legacy-unqualified', tracks=stored, sha256=digest,
                                        source_ref=SOURCE_REF, source_sha256=SOURCE_SHA, ready=True)

        catalog = self.publish_song()
        matches = [row for row in verified_catalog(catalog) if row['track_id'] == self.item.track_id]
        self.assertEqual(len(matches), 1)
        self.assertEqual((matches[0]['speed'], matches[0]['genre_group_codes']), (None, ['JAZZ']))
        self.assertIsNone(verified_catalog(legacy)[-1]['speed'])

    def test_failed_publication_rolls_back_track_and_video_candidate(self):
        with patch('backend.phase4_ops.registration_services.store_catalog',
                   side_effect=DomainError('CATALOG_INVALID', '카탈로그 저장 실패', 503)):
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(current_catalog().pk, self.base.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.catalog_version, '')
        self.assertIsNone(Track.objects.get(pk=self.item.track_id).speed)
        self.assertEqual(youtube_registry.track_registry(self.item.track_id)['mappings'], [])

    def test_publish_creates_immutable_catalog_and_is_idempotent(self):
        receipt_fields = ('result_id', 'receipt_id', 'payload_sha256', 'candidate', 'received_at')
        before = InferenceReceipt.objects.values(*receipt_fields).get(task=self.task)
        audio = {'energy': 0.6, 'acousticness': 0.5, 'valence': 0.7, 'source': '검수 데이터', 'mode': True}
        body = {**self.body, 'audio': audio, 'speed': 'medium'}
        key = str(uuid4())
        response = self.post(body, key=key)
        self.assertEqual(response.status_code, 200, response.content)
        repeat = self.post(body, key=key)
        self.assertEqual((repeat.status_code, repeat.json()), (response.status_code, response.json()))
        self.assertEqual(self.post({**body, 'genre_groups': ['ROCK']}, key=key).status_code, 409)
        self.assertEqual(self.post().status_code, 409)

        version = response.json()['registration']['catalog_version']
        catalog = Catalog.objects.get(pk=version)
        self.assertEqual(current_catalog().pk, version)
        self.assertEqual(Catalog.objects.filter(is_test=False, ready=True).count(), 2)
        previous = Catalog.objects.get(pk=self.base.pk)
        self.assertEqual((catalog.source_ref, catalog.source_sha256), (previous.source_ref, previous.source_sha256))
        self.assertEqual((previous.tracks, previous.sha256, previous.ready),
                         (self.base.tracks, self.base.sha256, True))
        rows = {row['track_id']: row for row in verified_catalog(catalog)}
        original_rows = {row['track_id']: row for row in verified_catalog(previous)}
        self.assertEqual({key: value for key, value in rows.items() if key != self.item.track_id}, original_rows)
        self.assertEqual(rows[self.item.track_id]['play_url'], VIDEO_URL)
        self.assertEqual(rows[self.item.track_id]['genre_group_codes'], ['JAZZ'])
        self.assertIsNone(rows[self.item.track_id]['speed'])
        self.assertNotIn('speed', response.json()['registration']['recommendation'])
        self.assertEqual(rows[self.item.track_id]['raw_energy'], '0.6')
        self.assertEqual(response.json()['registration']['recommendation']['audio'], audio)
        self.item.refresh_from_db()
        self.assertEqual(self.item.catalog_version, version)
        self.assertEqual(InferenceReceipt.objects.values(*receipt_fields).get(task=self.task), before)
        publication = History.objects.get(entity_id=str(self.task.pk), event_type='CATALOG_PUBLISHED')
        self.assertEqual(publication.data['receipt_id'], str(before['receipt_id']))
        self.assertEqual(publication.data['receipt_sha256'], before['payload_sha256'])
        self.assertEqual((publication.data['previous_version'], publication.data['catalog_sha256']),
                         (previous.pk, catalog.sha256))

    def test_rejects_unauthorized_stale_invalid_and_unapproved_publication(self):
        self.assertEqual(self.post(client=Client()).status_code, 401)
        self.assertEqual(self.post(client=self.client_for(self.ads_admin)).status_code, 403)
        stale = self.post({**self.body, 'expected_revision': self.task.revision - 1})
        self.assertEqual(stale.status_code, 409, stale.content)
        invalid_audio = {'energy': True, 'acousticness': 0.5, 'valence': 0.7, 'source': '검수 데이터'}
        for invalid in ({'genre_groups': ['NO_SUCH_GROUP']}, {'audio': invalid_audio}):
            with self.subTest(invalid=invalid):
                response = self.post({**self.body, **invalid})
                self.assertEqual(response.status_code, 422, response.content)
        Review.objects.filter(track_id=self.item.track_id).update(status='rejected')
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(current_catalog().pk, self.base.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.catalog_version, '')

    def test_requires_a_ready_base_catalog_without_mutating_the_approved_song(self):
        Catalog.objects.filter(pk=self.base.pk).update(ready=False)
        response = self.post()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIsNone(current_catalog())
        self.item.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.item.catalog_version, '')
        self.assertEqual(self.task.revision, self.body['expected_revision'])
        self.assertIsNone(Track.objects.get(pk=self.item.track_id).speed)
        self.assertEqual(youtube_registry.track_registry(self.item.track_id)['mappings'], [])

    def test_ordinary_source_publication_retains_the_eligible_approved_song(self):
        previous = self.publish_song()
        source = MusicSource.objects.create(sha256=SOURCE_SHA, schema='music-normalized19-v1', raw_path='test.csv')
        track = Track.objects.create(
            track_id='source-refresh-track', track_name='일반 소스 곡', artists_name='테스트 연주자',
            album=Album.objects.create(album_name='일반 앨범'), duration_ms=240_000,
            explicit=False, speed='medium', play_url='https://open.spotify.com/track/source-refresh-track',
        )
        Review.objects.create(track=track, status='approved')
        TrackOrigin.objects.create(track=track, source=source)
        genre = Tag.objects.create(tag_type='genre', tag_code='jazz', tag_name='Jazz', is_active=True)
        TrackTag.objects.create(track=track, tag=genre, review_status='approved')

        refreshed = publish('ordinary-source-refresh', SOURCE_REF, SOURCE_SHA)
        self.assertEqual(current_catalog().pk, refreshed.pk)
        self.assertTrue(Catalog.objects.filter(pk=previous.pk, ready=True).exists())
        self.assertEqual({row['track_id'] for row in verified_catalog(refreshed)}, {track.pk, self.item.track_id})
        Review.objects.filter(track_id=self.item.track_id).update(status='rejected')
        next_catalog = publish('ordinary-source-after-review', SOURCE_REF, SOURCE_SHA)
        self.assertEqual({row['track_id'] for row in verified_catalog(next_catalog)}, {track.pk})
        self.assertEqual({row['track_id'] for row in verified_catalog(refreshed)}, {track.pk, self.item.track_id})

    def test_existing_youtube_mapping_is_preserved(self):
        youtube_registry.register_mappings([{
            'track_id': self.item.track_id, 'video_url': VIDEO_URL, 'expected_revision': 0,
            'note': '기존 검수자 메모',
        }])
        youtube_registry.verify_mapping({'track_id': self.item.track_id, 'duration_ms': 240_000},
                                        VIDEO_ID, 1, True, True, 240)
        before = youtube_registry.track_registry(self.item.track_id)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()['video_review_pending'])
        self.assertEqual(youtube_registry.track_registry(self.item.track_id), before)

    @override_settings(OPENAI_PLAYLIST_ENABLED=False, OPENAI_PLAYLIST_LOCAL_RULE_FALLBACK=True)
    def test_new_song_is_selected_and_reviewed_video_resolves_for_iframe(self):
        catalog = self.publish_song()
        registry = youtube_registry.track_registry(self.item.track_id)
        self.assertEqual([(row['video_id'], row['status']) for row in registry['mappings']],
                         [(VIDEO_ID, 'CANDIDATE')])

        guest = Client()
        response = guest.post('/api/recommendation-jobs', {'weather_influence_mode': 'OFF'},
                              content_type='application/json', HTTP_IDEMPOTENCY_KEY='approved-song-playlist')
        self.assertEqual(response.status_code, 202, response.content)
        job_id = response.json()['job_id']
        self.assertEqual(Job.objects.get(pk=job_id).catalog_id, catalog.pk)
        self.assertTrue(run_one(queue='rule'))
        job = guest.get(f'/api/recommendation-jobs/{job_id}').json()
        self.assertEqual(job['status'], 'SUCCEEDED', job)
        playlist = guest.get(f"/api/playlists/{job['playlist_id']}").json()
        self.assertEqual(playlist['total_ms'], 10_800_000)
        self.assertIn(self.item.track_id, {row['track_id'] for row in playlist['items']})

        playback_path = f"/api/playlists/{job['playlist_id']}/youtube-playback"
        pending = guest.post(playback_path, {'track_id': self.item.track_id}, content_type='application/json')
        self.assertEqual(pending.json()['status'], 'MAPPING_REQUIRED')
        verified = self.admin_client.post('/api/admin/youtube-review/verify', {
            'track_id': self.item.track_id, 'video_id': VIDEO_ID, 'expected_revision': registry['revision'],
            'identity_confirmed': True, 'playback_observed': True, 'duration_seconds': 240,
        }, content_type='application/json')
        self.assertEqual(verified.status_code, 200, verified.content)
        playback = guest.post(playback_path, {'track_id': self.item.track_id}, content_type='application/json')
        self.assertEqual(playback.status_code, 200, playback.content)
        self.assertEqual(playback.json()['status'], 'READY')
        self.assertEqual(playback.json()['candidates'][0]['video_id'], VIDEO_ID)
