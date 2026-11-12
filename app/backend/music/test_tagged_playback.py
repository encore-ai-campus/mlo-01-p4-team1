from unittest.mock import patch

from django.test import TestCase, override_settings

from backend.recommendation import youtube_registry
from backend.recommendation.errors import DomainError
from backend.recommendation.models import YoutubeReviewAudit
from backend.recommendation.youtube_playback import resolve_playback_candidates
from .models import Album, Track
from .tagged_playback import register_tagged_youtube


@override_settings(LOCAL_DEMO=False, YOUTUBE_REGISTRY_BACKEND='database', YOUTUBE_PLAYBACK_MODE='KEYLESS')
class TaggedPlaybackTests(TestCase):
    video_id = 'ABCDEFGHIJK'
    actor = 'catalog-reviewer'

    @classmethod
    def setUpTestData(cls):
        cls.track = Track.objects.create(
            track_id='tagged-song', track_name='확인한 곡', artists_name='아티스트',
            album=Album.objects.create(album_name='앨범'), duration_ms=240_000, explicit=False,
            play_url=f'https://www.youtube.com/watch?v={cls.video_id}',
        )

    def attach(self, video_id=video_id, *, confirmed=True, duration=240_000):
        return register_tagged_youtube(
            self.track, video_id, reviewed_duration_ms=duration, actor=self.actor,
            identity_and_embed_confirmed=confirmed,
        )

    def test_operator_review_makes_only_the_confirmed_video_available_to_keyless_playback(self):
        mapping = self.attach()['mappings'][0]
        self.assertEqual((mapping['status'], mapping['duration_seconds'], mapping['native_verified']),
                         ('VERIFIED', 240, False))
        self.assertEqual(set(YoutubeReviewAudit.objects.values_list('actor', 'action')), {
            (self.actor, 'REGISTER_CANDIDATE'), (self.actor, 'VERIFY_OPERATOR_REPORTED_PLAYING'),
        })
        result = resolve_playback_candidates({
            'track_id': self.track.pk, 'track_name': self.track.track_name,
            'artists_name': self.track.artists_name, 'duration_ms': self.track.duration_ms,
        })
        self.assertEqual((result['status'], result['candidates'][0]['video_id']), ('READY', self.video_id))

    def test_incomplete_review_and_wrong_duration_leave_the_registry_unmodified(self):
        with self.assertRaisesMessage(DomainError, '원곡 일치') as incomplete:
            self.attach(confirmed=False)
        self.assertEqual(incomplete.exception.code, 'REVIEW_INCOMPLETE')
        with self.assertRaisesMessage(DomainError, '재생시간 차이') as mismatch:
            self.attach(duration=60_000)
        self.assertEqual(mismatch.exception.code, 'DURATION_MISMATCH')
        self.assertEqual(youtube_registry.track_registry(self.track.pk)['mappings'], [])
        self.assertFalse(YoutubeReviewAudit.objects.exists())

    def test_repeat_is_idempotent_and_different_video_cannot_replace_approval(self):
        approved = self.attach()
        self.assertEqual(self.attach(), approved)
        with self.assertRaises(DomainError) as conflict:
            self.attach('LMNOPQRSTUV')
        self.assertEqual(conflict.exception.code, 'VIDEO_MAPPING_CONFLICT')
        self.assertEqual(youtube_registry.track_registry(self.track.pk), approved)
        self.assertEqual(YoutubeReviewAudit.objects.count(), 2)

    def test_verification_failure_rolls_back_candidate_and_audit(self):
        with patch('backend.music.tagged_playback.youtube_registry.verify_mapping',
                   side_effect=DomainError('REGISTRY_UNAVAILABLE', '영상 연결 오류', 503)):
            with self.assertRaises(DomainError):
                self.attach()
        self.assertEqual(youtube_registry.track_registry(self.track.pk)['mappings'], [])
        self.assertFalse(YoutubeReviewAudit.objects.exists())
