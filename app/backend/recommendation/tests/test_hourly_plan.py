from datetime import datetime, timedelta, timezone
from fractions import Fraction
from time import monotonic
from unittest import TestCase
from unittest.mock import patch

from backend.recommendation.errors import DomainError
from backend.recommendation.hourly_plan import MINUTE_MS, plan_blocks


START = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)
VIDEO = 'M7lc1UVf-VE'


def song(name, minutes, quality=1, **fields):
    return {'track_id': name, 'duration_ms': minutes * MINUTE_MS,
            'scores': {'S': Fraction(quality)}, **fields}


def held(track):
    return {'tracks': [track], 'duration_ms': track['duration_ms']}


def ids(blocks):
    return [[track['track_id'] for track in block['tracks']] for block in blocks]


class HourlyPlanTests(TestCase):
    def test_initial_slots_have_distinct_forecasts_and_fixed_utc_bounds(self):
        pools = [[song(f'{slot}-{index}', 4) for index in range(24)] for slot in range(3)]
        forecasts = ['CLEAR', {'weather_code': 'RAIN', 'version': 'forecast-1'}, 'CLOUDY']
        with patch('backend.recommendation.hourly_plan._constraint_search') as search:
            result = plan_blocks(pools, start_at='2026-11-12T19:00:00+09:00',
                                 forecasts=forecasts, seed='fixed')
        search.assert_not_called()
        self.assertEqual([row['weather'] for row in result], forecasts)
        self.assertEqual([row['start_at'] for row in result],
                         [(START + timedelta(hours=slot)).isoformat() for slot in range(3)])
        self.assertEqual(result[-1]['end_at'], (START + timedelta(hours=3)).isoformat())
        self.assertEqual([row['duration_ms'] for row in result], [60 * MINUTE_MS] * 3)
        self.assertEqual(len({track for block in ids(result) for track in block}), 45)
        self.assertTrue(all(track in pools[index] for index, row in enumerate(result) for track in row['tracks']))

    def test_joint_duration_constraint_recovers_from_valid_but_short_individual_slots(self):
        pools = [[song(f'closest-{index}', 58, 2), song(f'long-{index}', 65)] for index in range(3)]
        result = plan_blocks(pools, start_at=START, forecasts=['RAIN'] * 3)
        self.assertTrue(all(55 * MINUTE_MS <= row['duration_ms'] <= 65 * MINUTE_MS for row in result))
        self.assertEqual(sum(row['duration_ms'] for row in result), 181 * MINUTE_MS)

    def test_regeneration_finds_fresh_song_when_greedy_would_reuse_all_tracks(self):
        pools = [[song(f'previous-{index}', 60, 10)] for index in range(3)]
        pools[2].append(song('fresh', 60))
        previous = {'previous-0', 'previous-1', 'previous-2'}
        result = plan_blocks(pools, start_at=START, forecasts=['RAIN'] * 3, previous_track_ids=previous)
        self.assertEqual(ids(result), [['previous-0'], ['previous-1'], ['fresh']])

    def test_append_preserves_kept_tracks_and_excludes_all_known_duplicate_identities(self):
        audio = 'a' * 64
        retained = [held(song('already', 60, youtube_video_id=VIDEO, recording_group_id='recording-1')),
                    held(song(f'tagged:{audio}', 60, isrc='ABC012345678'))]
        options = [
            song('already', 60, 10),
            song('same-video', 60, 9, play_url=f'https://youtu.be/{VIDEO}'),
            song('same-group', 60, 8, recording_group_id='recording-1'),
            song('same-audio', 60, 7, audio_sha256=audio.upper()),
            song('same-isrc', 60, 6, isrc='abc-01-23-45678'),
            song('new', 60),
        ]
        result = plan_blocks([options], start_at=START, forecasts=['RAIN'], retained=retained)
        self.assertEqual(ids(result), [['new']])
        self.assertEqual(result[0]['start_at'], START.isoformat())
        self.assertEqual(ids(retained), [['already'], [f'tagged:{audio}']])

    def test_joint_search_rejects_same_recording_across_initial_slots(self):
        pools = [[song(f'alias-{index}', 60, recording_group_id='one-recording')] for index in range(3)]
        with self.assertRaises(DomainError) as raised:
            plan_blocks(pools, start_at=START, forecasts=['RAIN'] * 3)
        self.assertEqual(raised.exception.code, 'CANDIDATE_SHORTAGE')
        self.assertEqual(raised.exception.details['scope'], 'supplied_pools')

    def test_append_uses_retained_actual_duration_to_choose_feasible_slot(self):
        retained = [held(song('kept-one', 55)), held(song('kept-two', 55))]
        options = [[song('too-short-for-window', 60, 10), song('fits-window', 65)]]
        result = plan_blocks(options, start_at=START, forecasts=['CLEAR'], retained=retained)
        self.assertEqual(ids(result), [['fits-window']])
        self.assertEqual(result[0]['duration_ms'], 65 * MINUTE_MS)

    def test_order_follows_last_retained_track_and_uses_quality_as_a_tiebreak(self):
        retained = [held(song('kept-one', 60)),
                    held(song('kept-two', 60, raw_energy='0.2', theme_codes=['CALM'], artists_name='A'))]
        options = [[song('similar-other-artist', 20, raw_energy='0.2', theme_codes=['CALM'], artists_name='B'),
                    song('similar-same-artist', 20, 2, raw_energy='0.2', theme_codes=['CALM'], artists_name='A'),
                    song('different', 20, 10, raw_energy='0.8', theme_codes=['PARTY'])]]
        result = plan_blocks(options, start_at=START, forecasts=['CLEAR'], retained=retained)
        self.assertEqual(ids(result), [['similar-other-artist', 'similar-same-artist', 'different']])

    def test_fixed_seed_is_stable_when_input_order_is_reversed_and_ignores_speed(self):
        pools = [[song(f'{slot}-{index}', 15, speed='fast' if index % 2 else 'slow')
                  for index in range(8)] for slot in range(3)]
        kwargs = dict(start_at=START, forecasts=['CLEAR'] * 3, seed='same')
        result = plan_blocks(pools, **kwargs)
        edited = [[{**track, 'speed': 'fastest'} for track in reversed(pool)] for pool in pools]
        self.assertEqual(ids(result), ids(plan_blocks(edited, **kwargs)))

    def test_expired_deadline_is_not_reported_as_candidate_shortage(self):
        with self.assertRaises(DomainError) as raised:
            plan_blocks([[]] * 3, start_at=START, forecasts=['RAIN'] * 3, deadline=monotonic() - 1)
        self.assertEqual(raised.exception.code, 'SEARCH_INCOMPLETE')

    def test_inconsistent_retained_duration_is_rejected(self):
        retained = [held(song('kept-one', 55)), {'tracks': [song('kept-two', 60)], 'duration_ms': 1}]
        with self.assertRaises(DomainError) as raised:
            plan_blocks([[song('new', 60)]], start_at=START, forecasts=['CLEAR'], retained=retained)
        self.assertEqual(raised.exception.code, 'VALIDATION_ERROR')
