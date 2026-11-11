from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from backend.common.models import Review, ReviewChange
from backend.music.models import Album, Track, TrackTag
from backend.recommendation.models import Catalog
from backend.workspaces.models import BusinessEntity, ServiceMembership, Workspace, WorkspaceMembership

from .models import InferenceReceipt, PipelineTask, SongItem
from .validation import fingerprint


ROOT = '/api/phase4'
VIDEO = 'https://www.youtube.com/watch?v=ABCDEFGHIJK'
WORKER_TOKEN = 'test-worker-token-with-24-characters'


@override_settings(ADMIN_PASSKEY_REQUIRED=True)
class SongRequestWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_user(username='request-owner')
        other_owner = users.create_user(username='request-other-owner')
        cls.manager = users.create_user(username='request-manager')
        cls.admin = users.create_user(username='music-operator', is_staff=True)
        cls.ads = users.create_user(username='ads-operator', is_staff=True)
        cls.admin.groups.add(Group.objects.get_or_create(name='OPS_ADMIN')[0])
        cls.ads.groups.add(Group.objects.get_or_create(name='ADS_ADMIN')[0])
        business = BusinessEntity.objects.create(registration_lookup='song-request-test',
                                                registration_ciphertext='', display_name='테스트 업체')
        cls.store = Workspace.objects.create(business=business, location_key='song-request-1',
                                             display_name='신청 매장', address_ciphertext='', status='ACTIVE')
        cls.other_store = Workspace.objects.create(business=business, location_key='song-request-2',
                                                   display_name='다른 매장', address_ciphertext='', status='ACTIVE')
        for user, role in ((cls.owner, 'OWNER'), (cls.manager, 'MANAGER')):
            ServiceMembership.objects.create(user=user, kind='BUSINESS')
            WorkspaceMembership.objects.create(user=user, workspace=cls.store, role=role)
        ServiceMembership.objects.create(user=other_owner, kind='BUSINESS')
        WorkspaceMembership.objects.create(user=other_owner, workspace=cls.other_store, role='OWNER')

    def client_for(self, user, passkey=False):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        if passkey:
            session = client.session
            session['admin_passkey_user'] = str(user.pk)
            session.save()
        return client

    def post(self, client, path, body, key=None):
        session = client.get(f'{ROOT}/session')
        return client.post(f'{ROOT}{path}', body, content_type='application/json',
                           HTTP_X_CSRFTOKEN=session.json()['csrf_token'], HTTP_IDEMPOTENCY_KEY=key or str(uuid4()))

    def songs(self, count):
        return {'venue_id': str(self.store.pk), 'items': [
            {'track_name': f'신청 곡 {i}', 'artist_name': '테스트 아티스트'} for i in range(count)
        ]}

    def test_twenty_per_request_and_ownership(self):
        owner = self.client_for(self.owner)
        session = owner.get(f'{ROOT}/session').json()
        self.assertEqual([row['venue_id'] for row in session['managed_venues']], [str(self.store.pk)])
        self.assertIsNone(session['song_policy']['period_limit'])
        self.assertEqual(self.post(owner, '/song-requests', self.songs(21)).status_code, 400)
        self.assertEqual(self.post(owner, '/song-requests', {**self.songs(1), 'venue_id': str(self.other_store.pk)}).status_code, 403)
        key = str(uuid4())
        accepted = self.post(owner, '/song-requests', self.songs(20), key)
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertEqual(self.post(owner, '/song-requests', self.songs(20), key).json(), accepted.json())
        self.assertEqual(SongItem.objects.count(), 20)
        self.assertEqual(self.client_for(self.manager).get(f'{ROOT}/song-requests').status_code, 403)

    def test_admin_checks_link_before_enqueue(self):
        owner = self.client_for(self.owner)
        item = self.post(owner, '/song-requests', self.songs(1)).json()['items'][0]
        path = f"/admin/song-items/{item['id']}/review"
        decision = {'expected_revision': item['revision'], 'decision': 'APPROVE'}
        self.assertEqual(owner.get(f'{ROOT}/admin/song-items').status_code, 403)
        self.assertEqual(self.client_for(self.admin).get(f'{ROOT}/admin/song-items').json()['code'], 'PASSKEY_REQUIRED')
        self.assertEqual(self.client_for(self.ads, passkey=True).get(f'{ROOT}/admin/song-items').status_code, 403)
        admin = self.client_for(self.admin, passkey=True)
        self.assertEqual(self.post(admin, path, decision).json()['code'], 'VIDEO_REQUIRED')
        existing_url = 'https://www.youtube.com/watch?v=ZZZZZZZZZZZ'
        track = Track.objects.create(track_id='test-existing-track', track_name='기존 곡', artists_name='기존 가수',
                                     album=Album.objects.create(album_name='기존 앨범'), duration_ms=120_000,
                                     explicit=False, play_url=existing_url)
        Review.objects.create(track=track, status='approved')
        self.assertEqual(self.post(admin, path, {**decision, 'youtube_url': existing_url}).json()['code'],
                         'DUPLICATE_TRACK')
        self.assertFalse(PipelineTask.objects.exists())
        response = self.post(admin, path, {**decision, 'youtube_url': 'https://youtu.be/ABCDEFGHIJK?t=7'})
        self.assertEqual(response.status_code, 200, response.content)
        task = PipelineTask.objects.get()
        self.assertEqual((task.status, task.source_ref, task.input_payload['youtube_url']), ('READY', VIDEO, VIDEO))
        self.assertEqual(admin.get(f'{ROOT}/admin/tasks').json()['items'][0]['result'], None)

    def receive_result(self, admin, owner):
        submission = self.post(owner, '/song-requests', self.songs(1)).json()
        item = submission['items'][0]
        accepted = self.post(admin, f"/admin/song-items/{item['id']}/review", {
            'decision': 'APPROVE', 'expected_revision': item['revision'], 'youtube_url': VIDEO,
        })
        self.assertEqual(accepted.status_code, 200, accepted.content)
        task = admin.get(f'{ROOT}/admin/tasks').json()['items'][0]
        claim = self.client.post(
            f"{ROOT}/worker/tasks/{task['id']}/claim", {}, content_type='application/json',
            secure=True, HTTP_AUTHORIZATION=f'Bearer {WORKER_TOKEN}',
        )
        self.assertEqual(claim.status_code, 200, claim.content)
        claimed = claim.json()
        payload = {
            'schema_version': 'phase4-inference-v1', 'batch_id': 'workflow-batch', 'result_id': 'workflow-result',
            **{key: claimed[key] for key in ('task_id', 'request_item_id', 'lease_token', 'track_id', 'source_ref')},
            'model_version': 'workflow-model', 'generated_at': timezone.now().isoformat(),
            'tags': {'genre': ['K-POP'], 'mood': ['CALM', 'EMOTIONAL']},
            'metadata': {'duration_ms': 242_125},
        }
        received = self.client.post(
            f'{ROOT}/worker/results', {**payload, 'payload_sha256': fingerprint(payload)},
            content_type='application/json', secure=True, HTTP_AUTHORIZATION=f'Bearer {WORKER_TOKEN}',
            HTTP_IDEMPOTENCY_KEY=payload['result_id'],
        )
        self.assertEqual(received.status_code, 201, received.content)
        tasks = admin.get(f'{ROOT}/admin/tasks').json()
        self.assertIn('ROCK_PUNK_METAL', tasks['tag_options']['genre'])
        task = tasks['items'][0]
        self.assertEqual(task['status'], 'RECEIVED_PENDING_REVIEW')
        self.assertIsNone(task['registration'])
        self.assertEqual(task['result']['metadata'], payload['metadata'])
        return submission, task, payload

    @override_settings(PHASE4_WORKER_TOKEN=WORKER_TOKEN)
    def test_final_approval_registers_edited_tags_in_music_list(self):
        owner = self.client_for(self.owner)
        admin = self.client_for(self.admin, passkey=True)
        submission, task, payload = self.receive_result(admin, owner)
        path = f"/admin/tasks/{task['id']}/finalize"
        edited = {'genre': ['K-POP', 'ROCK_PUNK_METAL'], 'mood': ['BRIGHT']}
        decision = {'expected_revision': task['revision'], 'tags': edited, 'album_name': '테스트 앨범',
                    'duration_ms': 242_125, 'explicit': False}
        self.assertEqual(admin.get('/api/admin/music/reviews').json()['items'], [])
        self.assertEqual(self.post(owner, path, decision).status_code, 403)
        self.assertEqual(admin.post(f'{ROOT}{path}', decision, content_type='application/json').status_code, 403)
        stale = {**decision, 'expected_revision': task['revision'] - 1}
        self.assertEqual(self.post(admin, path, stale).json()['code'], 'REVISION_CONFLICT')
        invalid = {**decision, 'tags': {'genre': ['NONEXISTENT'], 'mood': ['CALM']}}
        self.assertEqual(self.post(admin, path, invalid).json()['code'], 'INVALID_TAGS')
        self.assertFalse(Track.objects.exists())
        key = str(uuid4())
        response = self.post(admin, path, decision, key)
        self.assertEqual(response.status_code, 200, response.content)
        finalized = response.json()
        track_id = 'youtube:ABCDEFGHIJK'
        self.assertEqual(finalized['status'], 'REGISTERED')
        self.assertEqual(finalized['registration'], {'track_id': track_id, 'tags': edited})
        self.assertEqual(finalized['result']['tags'], payload['tags'])
        self.assertEqual(self.post(admin, path, decision, key).json(), finalized)
        self.assertEqual(self.post(admin, path, decision).json()['code'], 'REVISION_CONFLICT')

        reviews_url = '/api/admin/music/reviews'
        row = admin.get(reviews_url).json()['items'][0]
        self.assertEqual(row['track_name'], '신청 곡 0')
        self.assertEqual(row['review_status'], 'approved')
        self.assertEqual(row['genre_codes'], edited['genre'])
        self.assertEqual(row['mood_codes'], edited['mood'])
        self.assertEqual(admin.get(reviews_url, {'track_id': track_id}).json()['items'], [row])
        self.assertEqual(admin.get(reviews_url, {'status': 'approved'}).json()['items'], [row])
        self.assertEqual(admin.get(reviews_url, {'status': 'draft'}).json()['items'], [])
        track = Track.objects.select_related('album').get(pk=track_id)
        self.assertEqual((track.album.album_name, track.duration_ms, track.explicit, track.play_url),
                         ('테스트 앨범', 242_125, False, VIDEO))
        self.assertEqual(set(TrackTag.objects.filter(track=track).values_list('tag__tag_code', 'review_status')),
                         {('K-POP', 'approved'), ('ROCK_PUNK_METAL', 'approved'), ('BRIGHT', 'approved')})
        self.assertEqual(Track.objects.count(), 1)
        self.assertEqual(ReviewChange.objects.get(track_id=track_id).actor, self.admin)
        self.assertEqual(InferenceReceipt.objects.get(pk=payload['result_id']).candidate['tags'], payload['tags'])
        self.assertFalse(Catalog.objects.exists())
        detail = owner.get(f"{ROOT}/song-requests/{submission['id']}").json()
        self.assertEqual(detail['items'][0]['public_status'], '등록 완료')

    @override_settings(PHASE4_WORKER_TOKEN=WORKER_TOKEN)
    def test_final_approval_preserves_existing_draft_for_same_video(self):
        owner = self.client_for(self.owner)
        admin = self.client_for(self.admin, passkey=True)
        _, task, _ = self.receive_result(admin, owner)
        existing = Track.objects.create(
            track_id='unrelated-track', track_name='이미 등록된 곡', artists_name='기존 가수', play_url=VIDEO,
            album=Album.objects.create(album_name='기존 앨범'), duration_ms=180_000, explicit=False,
        )
        Review.objects.create(track=existing)
        response = self.post(admin, f"/admin/tasks/{task['id']}/finalize", {
            'expected_revision': task['revision'], 'tags': {'genre': ['K-POP'], 'mood': ['CALM']},
            'album_name': '테스트 앨범', 'duration_ms': 242_125, 'explicit': False,
        })
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()['code'], 'DUPLICATE_TRACK')
        self.assertEqual(list(Track.objects.values_list('pk', flat=True)), [existing.pk])
        self.assertEqual(Album.objects.count(), 1)
        self.assertFalse(ReviewChange.objects.exists())
        self.assertEqual(PipelineTask.objects.get(pk=task['id']).status, 'RECEIVED_PENDING_REVIEW')
