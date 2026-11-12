from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from backend.recommendation.catalog_storage import pack
from backend.recommendation.errors import DomainError
from backend.recommendation.hourly_catalog import candidate_rows, catalog_video_id, ensure_index
from backend.recommendation.models import Catalog, HourlyCatalogCandidate, HourlyCatalogIndex, HourlyCatalogTag
from backend.recommendation.tagged_preview import TAGGED_PREVIEW_SOURCE_PREFIX, full_tagged_preview_version


def track(track_id, genres, themes=(), **overrides):
    return {
        'track_id': track_id, 'track_name': f'곡 {track_id}', 'artists_name': '가수', 'album_name': '앨범',
        'duration_ms': 240_000, 'genre_group_codes': list(genres), 'theme_codes': list(themes),
        'instrumental': True, 'occasion_status': 'GENERAL', 'decision_source': 'HUMAN_REVIEW',
        'occasion_policy_version': 'test-policy', 'occasion_decision_version': 'test-decision', **overrides,
    }


def catalog(version, rows, **overrides):
    stored, digest = pack(rows)
    return Catalog.objects.create(**{
        'version': version, 'tracks': stored, 'sha256': digest,
        'source_ref': 'test-catalog', 'source_sha256': 'a' * 64, 'ready': True, **overrides,
    })


def ids(rows):
    return [row['track_id'] for row in rows]


class HourlyCatalogTests(TestCase):
    @override_settings(LOCAL_DEMO=True, INTEGRATION_ENABLED=False)
    def test_full_demo_keeps_missing_album_and_original_tags_and_rotates_the_shortlist(self):
        digest = 'a' * 64
        rows = [track(
            f'preview-{index}', ['ROCK_PUNK_METAL'] if index % 2 else [], album_name=None,
            occasion_status='SEASONAL_EVENT' if index % 2 else 'UNSCREENED',
            youtube_video_id=f'preview{index:04d}', play_url=f'https://www.youtube.com/watch?v=preview{index:04d}',
        ) for index in range(6)]
        full = catalog(full_tagged_preview_version(digest), rows, is_test=True,
                       source_ref=TAGGED_PREVIEW_SOURCE_PREFIX + digest)
        self.assertEqual(candidate_rows(full), rows)
        self.assertEqual(candidate_rows(full, genres=['ROCK_PUNK_METAL']), rows[1::2])
        self.assertEqual(candidate_rows(full, genres=['METAL']), [])
        self.assertEqual({row['track_id'] for request in range(30)
                          for row in candidate_rows(full, limit=1, rotation_seed=str(request))}, set(ids(rows)))
        with override_settings(LOCAL_DEMO=False):
            regular = catalog(full_tagged_preview_version('b' * 64), rows, is_test=True,
                              source_ref=TAGGED_PREVIEW_SOURCE_PREFIX + 'b' * 64, source_sha256='b' * 64)
            self.assertEqual(candidate_rows(regular), [])

    def test_sql_ranks_matching_mood_before_weather_and_relaxes_mood_within_genres(self):
        source = catalog('ranked', [
            track('untagged', ['JAZZ']),
            track('user', ['JAZZ'], ['CALM']),
            track('weather', ['ROCK'], ['PEACEFUL']),
            track('both', ['JAZZ', 'ROCK'], ['CALM', 'BRIGHT', 'PEACEFUL', 'CALM']),
            track('other-genre', ['POP'], ['CALM', 'PEACEFUL']),
            track('second-user', ['ROCK'], ['BRIGHT']),
        ])
        criteria = {'genres': ['JAZZ', 'ROCK'], 'themes': ['CALM', 'BRIGHT'], 'weather_themes': ['PEACEFUL']}
        self.assertEqual(ids(candidate_rows(source, **criteria, limit=4)),
                         ['both', 'user', 'second-user', 'weather'])
        self.assertEqual(ids(candidate_rows(source, **criteria, limit=4, allow_theme_fallback=False)),
                         ['both', 'user', 'second-user'])
        self.assertEqual(ids(candidate_rows(source, genres=['JAZZ'], weather_themes=['PEACEFUL'], limit=2)),
                         ['both', 'untagged'])
        self.assertEqual(HourlyCatalogTag.objects.filter(candidate__track_id='both', code='CALM').count(), 1)

    def test_filters_reviewed_vocal_exclusions_and_invalid_rows(self):
        source = catalog('filtered', [
            track('reviewed-vocal', ['JAZZ'], instrumental_review_value=False),
            track('reviewed-instrumental', ['JAZZ'], instrumental=False, instrumental_review_value=True),
            track('excluded', ['JAZZ']),
            track('seasonal', ['JAZZ'], occasion_status='SEASONAL_EVENT'),
            track('no-duration', ['JAZZ'], duration_ms=0),
            track('no-genre', []),
            track('no-album', ['JAZZ'], album_name=''),
        ])
        self.assertEqual(ids(candidate_rows(source, instrumental_only=True, excluded=['excluded'])),
                         ['reviewed-instrumental'])
        self.assertEqual(ids(candidate_rows(source, limit=1)), ['reviewed-vocal'])
        self.assertEqual(HourlyCatalogCandidate.objects.count(), 3)

    def test_reuses_completed_index_and_keeps_catalog_versions_isolated(self):
        original = track('same-id', ['JAZZ'], ['CALM'], recording_group_id='recording-1',
                         scores={'evidence': [1, 2]})
        version1 = catalog('v1', [original])
        version2 = catalog('v2', [track('same-id', ['POP'], ['BRIGHT'])])
        self.assertEqual(candidate_rows(version1, genres=['JAZZ']), [original])
        self.assertEqual(candidate_rows(version2, genres=['JAZZ']), [])
        with patch('backend.recommendation.worker.verified_catalog', side_effect=AssertionError('hot lookup rebuilt')):
            with CaptureQueriesContext(connection) as queries:
                results = candidate_rows(version1, genres=['JAZZ'], themes=['CALM'], limit=1)
            self.assertEqual(results, [original])
            self.assertEqual(len(queries), 2)
            self.assertIn('LIMIT 1', queries[1]['sql'])
            self.assertIn('recommendation_hourlycatalogtag', queries[1]['sql'])
            results[0]['scores']['evidence'].append(3)
            self.assertEqual(candidate_rows(version1, genres=['JAZZ']), [original])
        self.assertEqual(HourlyCatalogIndex.objects.count(), 2)

    def test_build_failure_leaves_no_marker_and_retry_builds_the_index(self):
        source = catalog('retry', [track('one', ['JAZZ'])])
        with patch('backend.recommendation.hourly_catalog.HourlyCatalogTag.objects.bulk_create',
                   side_effect=RuntimeError('fixture failed')):
            with self.assertRaisesRegex(RuntimeError, 'fixture failed'):
                ensure_index(source)
        self.assertFalse(HourlyCatalogIndex.objects.exists())
        self.assertFalse(HourlyCatalogCandidate.objects.exists())
        self.assertEqual(ids(candidate_rows(source)), ['one'])

    def test_rejects_unready_and_unverified_catalogs(self):
        unready = catalog('unready', [track('one', ['JAZZ'])], ready=False)
        wrong_hash = catalog('wrong-hash', [track('two', ['JAZZ'])], sha256='b' * 64)
        for source in (unready, wrong_hash):
            with self.assertRaises(DomainError) as error:
                candidate_rows(source)
            self.assertEqual(error.exception.code, 'CATALOG_INVALID')
        self.assertFalse(HourlyCatalogIndex.objects.exists())

    def test_snapshot_video_filter_does_not_promote_pending_mapping_or_url(self):
        pending = track('pending', ['JAZZ'], youtube_match_status='CANDIDATE', youtube_video_id='testVideo01',
                        play_url='https://www.youtube.com/watch?v=testVideo01')
        verified = track('verified', ['JAZZ'], youtube_match_status='VERIFIED', youtube_video_id='testVideo02')
        invalid = track('invalid', ['JAZZ'], youtube_match_status='VERIFIED', youtube_video_id='invalid',
                        play_url='https://www.youtube.com.example.com/watch?v=testVideo02')
        source = catalog('playback', [pending, verified, invalid])
        self.assertEqual(candidate_rows(source), [pending, verified, invalid])
        self.assertEqual(candidate_rows(source, require_playable=True), [verified])
        self.assertEqual(catalog_video_id(pending), 'testVideo01')
        self.assertEqual(catalog_video_id(verified), 'testVideo02')
        self.assertIsNone(catalog_video_id(invalid))
