import json
from datetime import timedelta
from time import monotonic, sleep
from unittest.mock import patch

from django.test import TestCase, override_settings

from backend.common.models import MusicSource, Review, TrackOrigin
from backend.music.catalog import publish
from backend.music.models import Album, Tag, Track, TrackTag
from backend.recommendation.errors import DomainError
from backend.recommendation.models import Job, Playlist
from backend.recommendation.worker import run_one
from pipeline.music.normalize import RAW_SCHEMA


@override_settings(
    LOCAL_DEMO=False,
    INTEGRATION_ENABLED=True,
    OWNER_RESOLVER='backend.common.identity.owner',
    EVENT_SINK='backend.common.events.record',
    GUEST_TTL_SECONDS=600,
    OPENAI_PLAYLIST_ENABLED=True,
    OPENAI_PLAYLIST_TIMEOUT_MS=30000,
    OPENAI_PLAYLIST_MAX_CANDIDATES=100,
    OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS=4096,
    OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS=0.001,
)
class GptSelectionIntegrationTests(TestCase):
    approved_ids = {f'approved-{index:02d}' for index in range(64)}

    @classmethod
    def setUpTestData(cls):
        source = MusicSource.objects.create(
            sha256='a' * 64, schema=RAW_SCHEMA, raw_path='fixture.csv',
        )
        album = Album.objects.create(album_name='추천 검증 앨범')
        genre = Tag.objects.create(tag_type='genre', tag_code='jazz', tag_name='재즈', is_active=True)
        tracks = Track.objects.bulk_create([
            Track(
                track_id=track_id, track_name=f'검증 곡 {track_id}', artists_name='검증 연주자',
                album=album, duration_ms=240000, explicit=False, mode=True, speed='medium',
                raw_energy='.6', raw_acousticness='.5', raw_valence='.7',
            )
            for track_id in sorted(cls.approved_ids | {'draft'})
        ])
        Review.objects.bulk_create([
            Review(track=track, status='approved' if track.pk in cls.approved_ids else 'draft')
            for track in tracks
        ])
        TrackOrigin.objects.bulk_create([TrackOrigin(track=track, source=source) for track in tracks])
        TrackTag.objects.bulk_create([
            TrackTag(track=track, tag=genre, review_status='approved') for track in tracks
        ])
        cls.catalog = publish('approved-fixture-v1', f'music-source:{source.pk}', source.pk)

    def setUp(self):
        self.sky = 1
        self.enterContext(patch.dict('os.environ', {'OPENAI_API_KEY': 'mock-only'}))
        self.enterContext(patch('backend.recommendation.service.obtain', side_effect=self.forecast))
        self.enterContext(patch('backend.recommendation.worker.obtain', side_effect=self.forecast))
        self.provider = self.enterContext(
            patch('backend.recommendation.worker.propose', side_effect=self.make_proposal),
        )

    def forecast(self, region_id, now):
        return {
            'region_id': region_id, 'region_name': '검증 지역',
            'as_of': now.isoformat(), 'valid_until': (now + timedelta(minutes=5)).isoformat(),
            'current': {'pty': 0, 'observed_at': now.isoformat()},
            'segments': [{
                'start_at': now.isoformat(), 'end_at': (now + timedelta(hours=4)).isoformat(),
                'issued_at': now.isoformat(), 'received_at': now.isoformat(),
                'pty': 0, 'sky': self.sky, 'status': 'valid',
            }],
        }

    def make_proposal(self, candidates, context, parent):
        previous_ids = set(parent or ())
        track_ids = sorted(
            (track['track_id'] for track in candidates),
            key=lambda track_id: (track_id in previous_ids, track_id),
        )[:45]
        return {
            'track_ids': track_ids, 'summary': '검증 선곡표',
            'track_reasons': [{'track_id': track_id, 'reason': '재즈 선곡'} for track_id in track_ids],
        }, {}

    def post(self, path, body, key):
        return self.client.post(path, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY=key)

    def enqueue(self, path, body, key):
        response = self.post(path, body, key)
        self.assertEqual(response.status_code, 202, response.content)
        job = Job.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.method, 'GPT_SELECTION')
        self.assertEqual(job.catalog_id, self.catalog.pk)
        return job

    def run_job(self, job, expected_status='SUCCEEDED'):
        deadline = monotonic() + 3
        while not run_one(queue='all'):
            if monotonic() >= deadline:
                self.fail(f'Worker did not claim job {job.pk}')
            sleep(0.01)
        job.refresh_from_db()
        self.assertEqual(job.status, expected_status, job.error)
        return job

    def first_playlist(self):
        job = self.enqueue('/api/recommendation-jobs', {
            'region_id': 'region:fixture', 'weather_influence_mode': 'ON',
            'selected_genre_codes': ['JAZZ'],
        }, 'first')
        self.run_job(job)
        return Playlist.objects.get(job=job)

    def test_initial_regeneration_and_changed_weather_use_the_same_approved_candidate_provider(self):
        with self.assertLogs('recommendation.worker', level='INFO') as worker_logs:
            first = self.first_playlist()
        finished = json.loads(worker_logs.records[-1].getMessage())
        self.assertEqual(finished['event'], 'recommendation_finished')
        timings = finished['stage_ms']
        self.assertEqual(set(timings), {
            'catalog', 'candidate_collection', 'candidate_window', 'provider_wait',
            'provider_round_trip', 'provider_validation', 'result_preparation', 'result_commit',
        })
        self.assertTrue(all(type(value) is int and value >= 0 for value in timings.values()))
        regen = self.enqueue(
            f'/api/playlists/{first.pk}/regenerations', {'exclusion_revision': 0}, 'regeneration',
        )
        self.run_job(regen)
        regenerated = Playlist.objects.get(job=regen)

        unchanged = self.post(
            f'/api/playlists/{regenerated.pk}/weather-refresh', {'exclusion_revision': 0}, 'unchanged',
        )
        self.assertEqual(unchanged.status_code, 200, unchanged.content)
        self.assertFalse(unchanged.json()['changed'])
        self.assertEqual(self.provider.call_count, 2)

        self.sky = 3
        refresh = self.enqueue(
            f'/api/playlists/{regenerated.pk}/weather-refresh', {'exclusion_revision': 0}, 'refresh',
        )
        self.run_job(refresh)
        refreshed = Playlist.objects.get(job=refresh)

        self.assertEqual(self.provider.call_count, 3)
        first_call, regen_call, refresh_call = self.provider.call_args_list
        first_ids = [item['track_id'] for item in first.snapshot['items']]
        for call, trigger, weather_code, parent in (
            (first_call, 'initial', 'CLEAR', None),
            (regen_call, 'regeneration', 'CLEAR', first_ids),
            (refresh_call, 'weather_refresh', 'CLOUDY', None),
        ):
            candidates, context, parent_ids = call.args
            self.assertEqual({track['track_id'] for track in candidates}, self.approved_ids)
            self.assertEqual(context['recommendation_trigger'], trigger)
            self.assertEqual(context['weather_code'], weather_code)
            self.assertEqual(parent_ids, parent)
        self.assertEqual(refreshed.family_id, first.family_id)
        self.assertEqual(refreshed.snapshot['context']['weather_code'], 'CLOUDY')

    def approved_playlist(self):
        first = self.first_playlist()
        approval = self.post(
            f'/api/playlists/{first.pk}/approval', {'approval_revision': 0, 'exclusion_revision': 0}, 'approval',
        )
        self.assertEqual(approval.status_code, 200, approval.content)
        return first

    def assert_original_retained(self, first, failed, error_code):
        polled = self.client.get(f'/api/recommendation-jobs/{failed.pk}')
        self.assertEqual(polled.status_code, 200, polled.content)
        self.assertEqual(polled.json()['error']['code'], error_code)
        self.assertIsNone(polled.json()['playlist_id'])
        self.assertEqual(self.provider.call_count, 2)
        self.assertEqual(Playlist.objects.count(), 1)
        detail = self.client.get(f'/api/playlists/{first.pk}')
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()['items'], first.snapshot['items'])
        self.assertEqual(detail.json()['approved_playlist_id'], first.pk)
        self.assertEqual(detail.json()['approval_revision'], 1)

    def test_provider_failure_during_regeneration_keeps_existing_approval(self):
        first = self.approved_playlist()
        regen = self.enqueue(
            f'/api/playlists/{first.pk}/regenerations', {'exclusion_revision': 0}, 'failed-regen',
        )
        self.provider.side_effect = DomainError('PROVIDER_UNAVAILABLE', '테스트 공급자 오류', 503)
        self.run_job(regen, 'FAILED')
        self.assert_original_retained(first, regen, 'PROVIDER_UNAVAILABLE')

    def test_unapproved_id_in_weather_refresh_keeps_existing_approval(self):
        first = self.approved_playlist()
        self.sky = 3
        refresh = self.enqueue(
            f'/api/playlists/{first.pk}/weather-refresh', {'exclusion_revision': 0}, 'invalid-refresh',
        )
        self.provider.side_effect = None
        self.provider.return_value = ({
            'track_ids': ['draft'], 'summary': '미승인 후보',
            'track_reasons': [{'track_id': 'draft', 'reason': '검증용 오류'}],
        }, {})
        self.run_job(refresh, 'FAILED')
        self.assert_original_retained(first, refresh, 'PROVIDER_RESPONSE_INVALID')
