from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import serialization as out
from .access import require_membership
from .catalog_port import existing_track, lookup
from .errors import require
from .models import InferenceReceipt, PipelineTask, SongItem, SongSubmission
from .policies import policy_status, song_policy
from .transactions import lock, record
from .validation import expected_revision, fingerprint, shape, song_inputs, text, youtube_url


def authorize(actor, policy, venue_id):
    require(venue_id is not None, 'VENUE_PERMISSION_REQUIRED', '승인된 매장을 선택해 주세요.', 403)
    venue_id = require_membership(actor.account_id, venue_id)
    key = 'venue:' + venue_id if policy.quota_scope == 'venue' else 'account:' + actor.account_id
    return venue_id, key


def quota(actor, venue_id=None, *, now=None):
    policy = song_policy(required=False)
    if policy is None:
        return {**policy_status(), 'used': None, 'remaining': None}
    if venue_id is None or policy.period_limit is None:
        if venue_id is not None:
            authorize(actor, policy, venue_id)
        return {**policy.public(), 'used': None, 'remaining': None}
    _, key = authorize(actor, policy, venue_id)
    now = now or timezone.now()
    used = SongItem.objects.filter(submission__quota_key=key,
                                   submission__created_at__gt=now - timedelta(days=policy.window_days),
                                   submission__created_at__lte=now).count()
    return {**policy.public(), 'used': used, 'remaining': max(0, policy.period_limit - used)}


def check(actor, data):
    policy = song_policy()
    venue_id, rows = song_inputs(data)
    authorize(actor, policy, venue_id)
    result = []
    for row in rows:
        matches = lookup({'track_name': row['track_name'], 'artist_name': row['artist_name'],
                          'youtube_url': row['youtube_url']})
        possible = SongItem.objects.filter(normalized_key=row['normalized_key'],
                                           review_status__in=['PENDING', 'HOLD', 'APPROVED']).exists()
        status = 'EXISTING_TRACK' if any(match.get('confirmed') is True for match in matches) else (
            'POSSIBLE' if matches or possible else 'NONE')
        result.append({**{k: v for k, v in row.items() if k not in {'normalized_key', 'exact_key'}},
                       'duplicate_status': status})
    return {'items': result, 'catalog_checked': True}


@transaction.atomic
def submit(actor, data):
    policy = song_policy()
    venue_id, rows = song_inputs(data)
    venue_id, quota_key = authorize(actor, policy, venue_id)
    lock('quota:' + quota_key)
    inspected = check(actor, data)
    require(not any(row['duplicate_status'] == 'EXISTING_TRACK' for row in inspected['items']),
            'DUPLICATE_TRACK', '확인된 기존 등록 곡이 포함되어 있습니다.', 409)
    require(not SongItem.objects.filter(submission__account_id=actor.account_id,
                                       exact_key__in=[row['exact_key'] for row in rows],
                                       review_status__in=['PENDING', 'HOLD', 'APPROVED']).exists(),
            'DUPLICATE_REQUEST', '동일한 내용으로 처리 중인 본인 신청이 있습니다.', 409)
    if policy.period_limit is not None:
        current = quota(actor, venue_id)
        require(current['used'] + len(rows) <= policy.period_limit, 'REQUEST_QUOTA_EXCEEDED',
                '기간 내 신청 한도를 초과했습니다.', 429, current)
    batch = SongSubmission.objects.create(account_id=actor.account_id, venue_id=venue_id,
                                          quota_key=quota_key, policy=policy.public())
    for position, row in enumerate(rows, 1):
        item = SongItem.objects.create(submission=batch, position=position, **row)
        record('song_item', item.pk, actor.account_id, 'SUBMITTED', item.revision)
    return out.submission(batch), 201


@transaction.atomic
def review(actor, item_id, data):
    require('review_songs' in actor.capabilities, 'PERMISSION_DENIED', '곡 검수 권한이 필요합니다.', 403)
    shape(data, ['decision', 'expected_revision', 'reason', 'internal_note', 'track_id', 'youtube_url'],
          ['decision', 'expected_revision'])
    decision = data['decision']
    require(decision in {'APPROVE', 'HOLD', 'REJECT', 'LINK_EXISTING'}, 'INVALID_INPUT', '검수 결정을 확인해 주세요.')
    row = SongItem.objects.select_for_update().select_related('submission').filter(pk=item_id).first()
    require(row is not None, 'NOT_FOUND', '신청 곡을 찾을 수 없습니다.', 404)
    require(row.revision == expected_revision(data['expected_revision']), 'REVISION_CONFLICT', '최신 검수 상태를 확인해 주세요.', 409)
    require(not row.catalog_version, 'ALREADY_PUBLISHED', '발행된 음악은 기존 음악 관리 절차에서 처리해 주세요.', 409)
    require(row.review_status in {'PENDING', 'HOLD'} or
            (row.review_status == 'APPROVED' and decision in {'HOLD', 'REJECT'}),
            'INVALID_STATE', '현재 상태에서 처리할 수 없는 결정입니다.', 409)
    reason = text(data.get('reason', ''), '공개 사유', 1000, optional=decision in {'APPROVE', 'LINK_EXISTING'})
    require(not InferenceReceipt.objects.filter(task__item=row).exists(), 'RESULT_ALREADY_RECEIVED',
            '이미 서버에 결과가 저장된 신청입니다.', 409)
    if decision == 'LINK_EXISTING':
        track_id = text(data.get('track_id', ''), '곡 ID', 128)
        existing_track(track_id)
        row.track_id = track_id
        row.review_status = 'LINKED'
    elif decision == 'APPROVE':
        confirmed_url = youtube_url(data.get('youtube_url', ''))
        require(bool(confirmed_url), 'VIDEO_REQUIRED', '곡과 일치하는 YouTube 링크를 확인해 입력해 주세요.')
        lock('song-source:' + fingerprint(confirmed_url))
        matches = lookup({'track_name': row.track_name, 'artist_name': row.artist_name, 'youtube_url': confirmed_url})
        require(not any(match['confirmed'] for match in matches), 'DUPLICATE_TRACK',
                '이미 등록된 영상입니다. 기존 곡으로 연결해 주세요.', 409)
        require(not PipelineTask.objects.filter(source_ref=confirmed_url, item__review_status='APPROVED').exclude(item=row).exists(),
                'DUPLICATE_SOURCE', '같은 영상의 승인된 작업이 있습니다.', 409)
        row.youtube_url = confirmed_url
        row.review_status = 'APPROVED'
        row.pipeline_status = 'READY'
        task, _ = PipelineTask.objects.get_or_create(item=row)
        require(task.status in {'INPUT_PENDING', 'CANCELLED'}, 'INVALID_STATE', '기존 처리 작업을 확인해 주세요.', 409)
        task.status = 'READY'
        task.source_ref = confirmed_url
        task.input_payload = {'youtube_url': confirmed_url, 'track_name': row.track_name, 'artist_name': row.artist_name}
        task.lease_token_hash = ''
        task.lease_expires_at = None
        task.revision += 1
        task.save()
        record('task', task.pk, actor.account_id, 'INPUT_PREPARED', task.revision)
    else:
        row.review_status = 'HOLD' if decision == 'HOLD' else 'REJECTED'
        task = PipelineTask.objects.select_for_update().filter(item=row).first()
        if task:
            task.status = 'CANCELLED'
            task.lease_token_hash = ''
            task.lease_expires_at = None
            task.revision += 1
            task.save()
        row.pipeline_status = 'NOT_QUEUED'
    row.public_reason = reason
    row.internal_note = text(data.get('internal_note', ''), '내부 메모', 2000, optional=True)
    row.reviewer_id = actor.account_id
    row.revision += 1
    row.save()
    record('song_item', row.pk, actor.account_id, row.review_status, row.revision)
    return out.song_item(row, operator=True), 200
