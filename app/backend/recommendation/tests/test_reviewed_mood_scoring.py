from unittest import TestCase

from backend.recommendation.inputs import DEFAULTS
from backend.recommendation.policy import (
    WEATHER_SCORE_VERSION, off_preference_details, precompute_scores, score,
)
from backend.recommendation.solver import choose


class ReviewedMoodScoringTests(TestCase):
    def setUp(self):
        self.legacy = {
            'track_id': 'legacy', 'duration_ms': 10_800_000, 'genre_group_codes': ['JAZZ'],
            'speed': 'slow', 'energy': 6, 'raw_energy': '.6', 'raw_acousticness': '.5',
            'raw_valence': '.7', 'mode': True,
        }
        self.reviewed = {**self.legacy, 'track_id': 'reviewed', 'theme_codes': ['CALM']}
        self.other = {**self.legacy, 'theme_codes': ['PARTY']}

    def test_selected_reviewed_tag_changes_solver_choice_without_changing_weather(self):
        for mode in ('OFF', 'ON'):
            with self.subTest(mode=mode):
                options = {**DEFAULTS, 'weather_influence_mode': mode, 'selected_theme_codes': ['CALM']}
                legacy_score = score(self.legacy, options, 'CLEAR')
                reviewed_score = score(self.reviewed, options, 'CLEAR')
                self.assertEqual(score(self.other, options, 'CLEAR'), legacy_score)
                self.assertEqual(reviewed_score['W'], legacy_score['W'])
                self.assertGreater(reviewed_score['S'], legacy_score['S'])
                selected = choose([
                    {**self.legacy, 'scores': legacy_score}, {**self.reviewed, 'scores': reviewed_score},
                ], 2, 5, mode=mode)
                self.assertEqual([track['track_id'] for track in selected], ['reviewed'])
                no_theme = {**options, 'selected_theme_codes': []}
                self.assertEqual(score(self.reviewed, no_theme, 'CLEAR'), score(self.legacy, no_theme, 'CLEAR'))

    def test_reviewed_tag_preserves_explicit_genre_and_ignores_legacy_speed(self):
        options = {**DEFAULTS, 'weather_influence_mode': 'OFF', 'selected_theme_codes': ['CALM'],
                   'selected_genre_codes': ['POP'], 'tempo_preference': 'fast'}
        evidence = off_preference_details(self.reviewed, options)
        self.assertEqual({key: value['score'] for key, value in evidence['components'].items()},
                         {'TAG': 100, 'G': 0})
        self.assertEqual(score(self.reviewed, options)['P'], 50)

    def test_any_reviewed_match_is_sufficient_for_off_when_audio_is_missing(self):
        sparse = {key: self.reviewed[key] for key in ('track_id', 'genre_group_codes', 'theme_codes')}
        selected = {**DEFAULTS, 'selected_theme_codes': ['BRIGHT', 'CALM']}
        off = {**selected, 'weather_influence_mode': 'OFF'}
        self.assertEqual(score(sparse, off)['P'], 100)
        self.assertEqual(off_preference_details(sparse, off)['theme'], 'CALM')
        self.assertIsNone(score({**sparse, 'theme_codes': []}, off)['P'])
        self.assertIsNone(score(sparse, selected, 'CLEAR'))

    def test_stale_theme_cache_is_ignored_and_recomputed_without_adding_tags(self):
        old = {'theme_version': 'OFF_THEME_AFFINITY_20261112_V2', 'theme': {'CALM': [1, 1]},
               'weather_version': WEATHER_SCORE_VERSION, 'weather': {'CLEAR': [100, 1]}}
        for track in (self.legacy, self.reviewed):
            with self.subTest(track=track['track_id']):
                cached = precompute_scores({**track, 'score_cache': old})
                for mode in ('OFF', 'ON'):
                    options = {**DEFAULTS, 'weather_influence_mode': mode, 'selected_theme_codes': ['CALM']}
                    expected = score(track, options, 'CLEAR')
                    self.assertEqual(score({**track, 'score_cache': old}, options, 'CLEAR'), expected)
                    self.assertEqual(score(cached, options, 'CLEAR'), expected)
                self.assertEqual(cached.get('theme_codes'), track.get('theme_codes'))
                self.assertNotIn('mood', cached)
