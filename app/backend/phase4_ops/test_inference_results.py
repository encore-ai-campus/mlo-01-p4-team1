import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from backend.common.models import Review
from backend.music.models import Track, TrackTag
from backend.recommendation.models import Catalog
from backend.workspaces.models import BusinessEntity, ServiceMembership, Workspace, WorkspaceMembership

from .access import Principal
from .errors import OperationError
from .models import History, InferenceReceipt, PipelineTask, SongItem, SongSubmission
from .pipeline_services import claim
from .song_services import review


WORKER_TOKEN = 'test-worker-token-with-24-characters'


@override_settings(PHASE4_WORKER_TOKEN=WORKER_TOKEN, PHASE4_LEASE_SECONDS=300)
class InferenceResultTests(TestCase):
    def setUp(self):
        business = BusinessEntity.objects.create(
            registration_lookup='test-registration', registration_ciphertext='', display_name='테스트 업체',
        )
        workspace = Workspace.objects.create(
            business=business, location_key='test-location', display_name='테스트 매장',
            address_ciphertext='', status='ACTIVE',
        )
        owner = get_user_model().objects.create_user(username='inference-test-owner')
        ServiceMembership.objects.create(user=owner, kind='BUSINESS')
        WorkspaceMembership.objects.create(user=owner, workspace=workspace, role='OWNER')
        submission = SongSubmission.objects.create(
            account_id=str(owner.pk), quota_key='owner-1', venue=workspace,
        )
        self.item = SongItem.objects.create(
            submission=submission,
            position=1,
            track_name='테스트 신곡',
            artist_name='테스트 아티스트',
            youtube_url='https://www.youtube.com/watch?v=ABCDEFGHIJK',
            normalized_key='normalized-key',
            exact_key='exact-key',
        )
        self.operator = Principal('operator-1', frozenset({'review_songs'}))
        review(self.operator, self.item.pk, {
            'decision': 'APPROVE',
            'expected_revision': self.item.revision,
            'youtube_url': self.item.youtube_url,
        })
        self.task = PipelineTask.objects.get(item=self.item)
        self.claimed, _ = claim(self.task.pk)

    def result(self, **changes):
        data = {
            'schema_version': 'phase4-inference-v1',
            'result_id': 'test-result-1',
            'batch_id': 'test-batch-1',
            **{key: self.claimed[key] for key in (
                'task_id', 'request_item_id', 'lease_token', 'track_id', 'source_ref',
            )},
            'model_version': 'test-mert-ensemble-v1',
            'generated_at': timezone.now().isoformat(),
            'tags': {
                'genre': ['K-POP', 'ROCK_PUNK_METAL', 'FOLK_COUNTRY_BLUES'],
                'mood': ['CALM', 'GROOVY'],
            },
            **changes,
        }
        raw = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
        ).encode('utf-8')
        return {**data, 'payload_sha256': hashlib.sha256(raw).hexdigest()}

    def post(self, data, expected, *, token=WORKER_TOKEN, key=None):
        response = self.client.post(
            '/api/phase4/worker/results',
            data=json.dumps(data).encode('utf-8'),
            content_type='application/json',
            secure=True,
            HTTP_AUTHORIZATION=f'Bearer {token}',
            HTTP_IDEMPOTENCY_KEY=data['result_id'] if key is None else key,
        )
        self.assertEqual(response.status_code, expected, response.content.decode('utf-8'))
        return json.loads(response.content)

    def available_task_ids(self):
        response = self.client.get(
            '/api/phase4/worker/tasks', secure=True, HTTP_AUTHORIZATION=f'Bearer {WORKER_TOKEN}',
        )
        self.assertEqual(response.status_code, 200, response.content.decode('utf-8'))
        return [row['id'] for row in json.loads(response.content)['items']]

    def test_success_preserves_model_tags_and_exact_retries_without_publishing(self):
        payload = self.result()
        ack = self.post(payload, 201)
        saved = InferenceReceipt.objects.get(pk=payload['result_id'])
        self.assertEqual(ack, {
            'result_id': saved.pk,
            'payload_sha256': payload['payload_sha256'],
            'receipt_id': str(saved.receipt_id),
            'status': 'RECEIVED_PENDING_REVIEW',
        })
        self.assertEqual(saved.candidate, {key: value for key, value in payload.items() if key != 'lease_token'})
        self.task.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.task.status, 'RECEIVED_PENDING_REVIEW')
        self.assertEqual(self.item.pipeline_status, self.task.status)
        self.assertEqual(self.task.lease_token_hash, '')
        self.assertIsNone(self.task.lease_expires_at)
        with self.settings(PHASE4_TAG_CODES=None):
            self.assertEqual(self.post(payload, 200), ack)
        self.assertEqual(InferenceReceipt.objects.count(), 1)
        self.assertEqual(History.objects.filter(event_type='RESULT_RECEIVED').count(), 1)
        self.assertEqual(self.available_task_ids(), [])
        with self.assertRaises(OperationError) as rejected:
            review(self.operator, self.item.pk, {
                'decision': 'HOLD',
                'expected_revision': self.item.revision,
                'reason': '결과 도착 뒤 보류를 시도합니다.',
            })
        self.assertEqual(rejected.exception.code, 'RESULT_ALREADY_RECEIVED')
        self.assertFalse(Track.objects.exists())
        self.assertFalse(TrackTag.objects.exists())
        self.assertFalse(Review.objects.exists())
        self.assertFalse(Catalog.objects.exists())

    def test_rejects_unknown_duplicate_and_malformed_tag_codes(self):
        for tags in (
            {'genre': ['AFROBEAT'], 'mood': []},
            {'genre': ['K-POP', 'K-POP'], 'mood': []},
            {'genre': ['k-pop'], 'mood': []},
            {'genre': [True], 'mood': []},
            {'genre': [], 'mood': ['UNKNOWN']},
        ):
            with self.subTest(tags=tags):
                self.assertEqual(self.post(self.result(tags=tags), 422)['code'], 'INVALID_TAGS')
        obsolete_shape = self.result(tags={'mood': [], 'instrument': []})
        self.assertEqual(self.post(obsolete_shape, 400)['code'], 'INVALID_INPUT')
        self.assertFalse(InferenceReceipt.objects.exists())

    def test_new_receipt_requires_a_configured_tag_dictionary(self):
        with self.settings(PHASE4_TAG_CODES=None):
            self.assertEqual(self.post(self.result(), 503)['code'], 'TAG_DICTIONARY_NOT_READY')
        self.assertFalse(InferenceReceipt.objects.exists())

    def test_rejects_tampering_and_mismatched_task_source_or_track(self):
        payload = self.result()
        self.assertEqual(self.post(payload, 401, token='wrong')['code'], 'AUTH_REQUIRED')
        self.assertEqual(self.post(payload, 400, key='wrong')['code'], 'IDEMPOTENCY_KEY_REQUIRED')
        modified = {**payload, 'model_version': 'changed-after-signing'}
        self.assertEqual(self.post(modified, 422)['code'], 'RESULT_HASH_MISMATCH')
        wrong_item = self.result(request_item_id=str(uuid4()))
        self.assertEqual(self.post(wrong_item, 404)['code'], 'TASK_NOT_FOUND')
        for changes in (
            {'source_ref': 'https://www.youtube.com/watch?v=12345678901'},
            {'track_id': 'unrelated-track'},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(self.result(**changes), 409)['code'], 'SOURCE_MISMATCH')
        self.assertFalse(InferenceReceipt.objects.exists())

    def test_rejects_conflicting_replay_and_second_result_for_same_task(self):
        payload = self.result()
        self.post(payload, 201)
        conflict = self.result(model_version='different-version')
        self.assertEqual(self.post(conflict, 409)['code'], 'RESULT_CONFLICT')
        another = self.result(result_id='test-result-2')
        self.assertEqual(self.post(another, 409)['code'], 'TASK_NOT_ACTIVE')
        self.assertEqual(InferenceReceipt.objects.count(), 1)

    def test_rejects_expired_lease_and_allows_worker_to_claim_again(self):
        expired = self.result()
        PipelineTask.objects.filter(pk=self.task.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.available_task_ids(), [str(self.task.pk)])
        self.assertEqual(self.post(expired, 409)['code'], 'LEASE_EXPIRED')
        self.claimed, _ = claim(self.task.pk)
        self.assertEqual(self.available_task_ids(), [])
        self.assertEqual(self.post(expired, 409)['code'], 'LEASE_EXPIRED')
        self.post(self.result(result_id='test-result-2'), 201)
        self.assertEqual(InferenceReceipt.objects.count(), 1)

    def test_admin_hold_cancels_claimed_job_and_excludes_it_from_worker_list(self):
        payload = self.result()
        self.item.refresh_from_db()
        review(self.operator, self.item.pk, {
            'decision': 'HOLD',
            'expected_revision': self.item.revision,
            'reason': '링크를 다시 확인합니다.',
        })
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'CANCELLED')
        self.assertEqual(self.task.lease_token_hash, '')
        self.assertIsNone(self.task.lease_expires_at)
        self.assertEqual(self.post(payload, 409)['code'], 'TASK_NOT_ACTIVE')
        self.assertEqual(self.available_task_ids(), [])
        self.assertFalse(InferenceReceipt.objects.exists())
