import os
from datetime import timedelta
from threading import Event
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from backend.recommendation.catalog_storage import pack
from backend.recommendation.gpt_order import GptOrderError
from backend.recommendation.models import Catalog, Job, Playlist
from backend.recommendation.worker import run_one, verified_catalog


def forecast(region, now):
    states = [(0, 1), (1, 4), (0, 3), (3, 4)]
    return {
        'region_id': region, 'as_of': now.isoformat(),
        'valid_until': (now + timedelta(minutes=5)).isoformat(),
        'current': {'pty': 0, 'observed_at': now.isoformat()},
        'segments': [
            {'start_at': (now + timedelta(hours=i)).isoformat(),
             'end_at': (now + timedelta(hours=i + 1)).isoformat(),
             'issued_at': (now - timedelta(minutes=10)).isoformat(),
             'received_at': (now - timedelta(minutes=9)).isoformat(),
             'pty': states[i % 4][0], 'sky': states[i % 4][1], 'status': 'valid'}
            for i in range(6)
        ],
    }


@override_settings(
    LOCAL_DEMO=True, INTEGRATION_ENABLED=False, HOURLY_RECOMMENDATION_ENABLED=True,
    WEATHER_PROVIDER='', GEMINI_ENABLED=False, HOURLY_RECOMMENDATION_BUDGET_SECONDS=10,
)
@patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only', 'OPENAI_PLAYLIST_ORDER_MODEL': ''})
class HourlyJobsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_c_demo', verbosity=0)
        demo = Catalog.objects.get(pk='c-demo-v3')
        rows = verified_catalog(demo)
        themes = ('BRIGHT', 'CALM', 'PEACEFUL', 'DREAMY')
        rows = [{**rows[i % len(rows)], 'track_id': f'hourly-{i:03d}',
                 'theme_codes': [themes[i // 20]], 'duration_ms': 240_000}
                for i in range(80)]
        stored, digest = pack(rows)
        Catalog.objects.create(version='hourly-fixture', tracks=stored, sha256=digest,
                               source_ref='fixture', source_sha256='b' * 64, ready=True, is_test=True)

    def setUp(self):
        self.client = Client()
        self.client.get('/api/c-demo/context')

    def post(self, path, body, key):
        return self.client.post(path, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY=key)

    def complete(self, response):
        self.assertEqual(response.status_code, 202, response.content)
        self.assertTrue(run_one(queue='rule'))
        job = Job.objects.get(pk=response.json()['job_id'])
        self.assertEqual(job.status, 'SUCCEEDED', job.error)
        return Playlist.objects.get(job=job)

    def test_initial_and_append_each_call_once_with_weather_and_retained_blocks(self):
        def reverse(blocks, **kwargs):
            return [[row['track_id'] for row in reversed(block['tracks'])] for block in blocks]

        now = timezone.now()
        with patch('backend.recommendation.hourly_jobs.obtain', side_effect=forecast), \
             patch('backend.recommendation.hourly_jobs.propose_order', side_effect=reverse) as provider:
            with patch('django.utils.timezone.now', return_value=now):
                initial = self.complete(self.post('/api/recommendation-jobs', {'region_id': 'demo-region'}, 'hourly-initial'))
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(provider.call_args.kwargs['timeout_seconds'], 9)
                plan = initial.snapshot['hourly_plan']
                self.assertEqual([block['weather'] for block in plan['blocks']], ['CLEAR', 'RAIN', 'CLOUDY'])
                self.assertEqual(initial.snapshot['provider_diagnostics']['status'], 'accepted')
                self.assertEqual(initial.snapshot['model'], 'gpt-6-luna')
                first_ids = set(plan['blocks'][0]['track_ids'])
                self.assertGreater(sum('BRIGHT' in row['theme_codes'] for row in initial.snapshot['items']
                                       if row['track_id'] in first_ids), len(first_ids) // 2)
                self.assertEqual(initial.snapshot['items'][0]['track_id'],
                                 provider.call_args.args[0][0]['tracks'][-1]['track_id'])
                early = self.post(f'/api/playlists/{initial.pk}/weather-refresh', {'exclusion_revision': 0}, 'early')
                self.assertEqual(early.status_code, 200)
            with patch('django.utils.timezone.now', return_value=now + timedelta(hours=1, seconds=1)):
                refresh = f'/api/playlists/{initial.pk}/weather-refresh'
                queued = self.post(refresh, {'exclusion_revision': 0}, 'hourly-append')
                self.assertEqual(self.post(refresh, {'exclusion_revision': 0}, 'hourly-append').json(), queued.json())
                self.assertEqual(self.post(refresh, {'exclusion_revision': 0}, 'other').status_code, 409)
                updated = self.complete(queued)
                self.assertEqual(provider.call_count, 2)
                self.assertEqual(len(provider.call_args.args[0]), 1)
                blocks = updated.snapshot['hourly_plan']['blocks']
                self.assertEqual(blocks[:2], plan['blocks'][1:])
                self.assertEqual(provider.call_args.kwargs['anchor']['track_id'], blocks[1]['track_ids'][-1])
                self.assertEqual(self.post(refresh, {'exclusion_revision': 0}, 'stale').status_code, 409)
                ids = [item['track_id'] for item in updated.snapshot['items']]
                self.assertEqual(len(ids), len(set(ids)))

    def test_manual_regeneration_orders_three_new_blocks_without_gemini(self):
        def same_order(blocks, **kwargs):
            return [[row['track_id'] for row in block['tracks']] for block in blocks]

        with patch('backend.recommendation.hourly_jobs.propose_order', side_effect=same_order) as provider:
            initial = self.complete(self.post('/api/recommendation-jobs', {'weather_influence_mode': 'OFF'},
                                              'initial-for-regeneration'))
            previous_ids = {row['track_id'] for row in initial.snapshot['items']}
            excluded = initial.snapshot['items'][0]['track_id']
            changed = self.client.put(
                f'/api/playlist-families/{initial.family_id}/exclusions',
                {'source_playlist_id': initial.pk, 'expected_revision': 0, 'excluded_track_ids': [excluded]},
                content_type='application/json', HTTP_IDEMPOTENCY_KEY='exclude-before-regeneration',
            )
            self.assertEqual(changed.status_code, 200)
            body = {'exclusion_revision': changed.json()['revision']}
            regenerated = self.complete(self.post(f'/api/playlists/{initial.pk}/regenerations', body, 'regenerate'))
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(len(provider.call_args.args[0]), 3)
            self.assertIsNone(provider.call_args.kwargs['anchor'])
            self.assertEqual(regenerated.snapshot['provider_diagnostics']['status'], 'accepted')
            self.assertEqual(regenerated.snapshot['model'], 'gpt-6-luna')
            self.assertEqual(regenerated.job.catalog_id, initial.job.catalog_id)
            selected = {row['track_id'] for row in regenerated.snapshot['items']}
            self.assertNotIn(excluded, selected)
            self.assertGreaterEqual(len(selected - previous_ids), max(1, len(previous_ids) // 5))

    @patch('backend.recommendation.hourly_jobs.propose_order', side_effect=GptOrderError('test timeout'))
    def test_one_failed_call_uses_valid_rule_playlist(self, provider):
        playlist = self.complete(self.post('/api/recommendation-jobs',
                                           {'weather_influence_mode': 'OFF'}, 'hourly-fallback'))
        provider.assert_called_once()
        self.assertEqual(playlist.snapshot['provider_diagnostics']['status'], 'rule_fallback')
        self.assertEqual(len(playlist.snapshot['hourly_plan']['blocks']), 3)
        self.assertTrue(10_500_000 <= playlist.snapshot['total_ms'] <= 11_100_000)

    @override_settings(HOURLY_GPT_TIMEOUT_SECONDS=0.25)
    def test_stalled_provider_does_not_block_rule_result(self):
        release = Event()
        returned = Event()

        def stall(blocks, **kwargs):
            try:
                release.wait(4)
                return [[row['track_id'] for row in block['tracks']] for block in blocks]
            finally:
                returned.set()

        try:
            with patch('backend.recommendation.hourly_jobs.propose_order', side_effect=stall) as provider:
                playlist = self.complete(self.post('/api/recommendation-jobs',
                                                   {'weather_influence_mode': 'OFF'}, 'hourly-stalled'))
                provider.assert_called_once()
                self.assertFalse(returned.is_set())
                self.assertEqual(playlist.snapshot['provider_diagnostics']['status'], 'rule_fallback')
        finally:
            release.set()
            self.assertTrue(returned.wait(1))
