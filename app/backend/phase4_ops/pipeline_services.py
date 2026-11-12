import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .errors import require
from .models import InferenceReceipt, PipelineTask, SongItem
from .transactions import lock, record
from .validation import fingerprint, shape, text, track_duration


TAG_KINDS = ('genre', 'mood')
TAG_CODE = re.compile(r'[A-Z][A-Z0-9_-]{0,63}')


def locked_task(task_id):
    """Every path locks item before task, matching the review lock order."""
    item_id = PipelineTask.objects.filter(pk=task_id).values_list('item_id', flat=True).first()
    require(item_id is not None, 'NOT_FOUND', '작업을 찾을 수 없습니다.', 404)
    SongItem.objects.select_for_update().get(pk=item_id)
    return PipelineTask.objects.select_for_update().select_related('item').get(pk=task_id)


def locked_review_result(task_id, expected_revision):
    task = locked_task(task_id)
    require(task.revision == expected_revision, 'REVISION_CONFLICT', '최신 검수 상태를 확인해 주세요.', 409)
    receipt = getattr(task, 'receipt', None)
    item = task.item
    require(task.is_current and task.status == item.pipeline_status == 'RECEIVED_PENDING_REVIEW' and
            item.review_status == 'APPROVED' and receipt is not None and not item.track_id,
            'INVALID_STATE', '최종 검수 대기 중인 결과만 처리할 수 있습니다.', 409)
    return task, receipt


@transaction.atomic
def claim(task_id):
    task = locked_task(task_id)
    now = timezone.now()
    require(task.is_current and task.item.review_status == 'APPROVED' and (
        task.status == 'READY' or (task.status == 'RUNNING' and task.lease_expires_at is not None
                                   and task.lease_expires_at <= now)),
        'TASK_NOT_READY', '처리 가능한 승인 작업이 아닙니다.', 409)
    seconds = getattr(settings, 'PHASE4_LEASE_SECONDS', 300)
    require(type(seconds) is int and 30 <= seconds <= 3600, 'POLICY_NOT_READY', '작업 임대 설정을 확인해 주세요.', 503)
    token = secrets.token_urlsafe(32)
    task.lease_token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    task.lease_expires_at = now + timedelta(seconds=seconds)
    task.status = 'RUNNING'
    task.attempts += 1
    task.revision += 1
    task.save()
    SongItem.objects.filter(pk=task.item_id).update(pipeline_status='RUNNING')
    record('task', task.pk, 'worker', 'CLAIMED', task.revision, attempt=task.attempts)
    return {'task_id': str(task.pk), 'request_item_id': str(task.item_id), 'lease_token': token,
            'lease_expires_at': task.lease_expires_at.isoformat(), 'source_ref': task.source_ref,
            'input_payload': task.input_payload, 'track_id': task.item.track_id or None}, 200


def validate_result(data):
    fields = ['schema_version', 'result_id', 'batch_id', 'task_id', 'request_item_id', 'lease_token',
              'track_id', 'source_ref', 'model_version', 'generated_at', 'tags', 'payload_sha256']
    shape(data, [*fields, 'metadata'], fields)
    require(data['schema_version'] == 'phase4-inference-v1', 'INVALID_SCHEMA', '추론 결과 규격을 확인해 주세요.', 422)
    for field in ['result_id', 'batch_id', 'lease_token', 'model_version']:
        text(data[field], field, 128)
    text(data['source_ref'], 'source_ref', 500)
    if data['track_id'] is not None:
        text(data['track_id'], 'track_id', 128)
    try:
        task_id = uuid.UUID(data['task_id'])
        item_id = uuid.UUID(data['request_item_id'])
        generated = datetime.fromisoformat(data['generated_at'])
        require(generated.tzinfo is not None and generated.utcoffset() == timedelta(0),
                'INVALID_RESULT', '생성 시각은 UTC 형식이어야 합니다.', 422)
    except (TypeError, ValueError, AttributeError):
        require(False, 'INVALID_RESULT', '결과 식별자·시각 형식을 확인해 주세요.', 422)
    validate_tags(data['tags'])
    if 'metadata' in data:
        metadata = shape(data['metadata'], ['duration_ms', 'album_name', 'explicit'])
        if 'duration_ms' in metadata:
            track_duration(metadata['duration_ms'])
        if 'album_name' in metadata:
            text(metadata['album_name'], '앨범명')
        if 'explicit' in metadata:
            require(type(metadata['explicit']) is bool,
                    'INVALID_METADATA', '청소년 유해 표시 값을 확인해 주세요.', 422)
    digest = data['payload_sha256']
    require(type(digest) is str and re.fullmatch('[0-9a-f]{64}', digest) is not None,
            'RESULT_HASH_MISMATCH', '결과 해시 형식을 확인해 주세요.', 422)
    require(hmac.compare_digest(digest, fingerprint({k: v for k, v in data.items() if k != 'payload_sha256'})),
            'RESULT_HASH_MISMATCH', '결과 내용의 해시가 다릅니다.', 422)
    return task_id, item_id


def validate_tags(tags, *, minimum=0):
    shape(tags, TAG_KINDS, TAG_KINDS)
    for kind in TAG_KINDS:
        codes = tags[kind]
        require(type(codes) is list and minimum <= len(codes) <= 32 and all(type(code) is str and
                TAG_CODE.fullmatch(code) for code in codes),
                'INVALID_TAGS', '태그 코드 형식을 확인해 주세요.', 422)
        require(len(codes) == len(set(codes)), 'INVALID_TAGS', '중복 태그가 있습니다.', 422)


def tag_dictionary(*, required=True):
    dictionary = getattr(settings, 'PHASE4_TAG_CODES', None)
    valid = (type(dictionary) is dict and set(dictionary) == set(TAG_KINDS)
             and all(isinstance(codes, (list, tuple, frozenset)) and bool(codes)
                     and all(type(code) is str and TAG_CODE.fullmatch(code) for code in codes)
                     and len(codes) == len(set(codes)) for codes in dictionary.values()))
    if required:
        require(valid, 'TAG_DICTIONARY_NOT_READY', '추론 태그 사전 연결이 필요합니다.', 503)
    return dictionary if valid else {}


def validate_dictionary(tags):
    dictionary = tag_dictionary()
    for kind, codes in tags.items():
        require(set(codes) <= set(dictionary[kind]), 'INVALID_TAGS', '태그 사전에 없는 값이 있습니다.', 422)


@transaction.atomic
def receive(data):
    task_id, item_id = validate_result(data)
    lock('result:' + data['result_id'])
    old = InferenceReceipt.objects.filter(pk=data['result_id']).first()
    if old:
        require(old.payload_sha256 == data['payload_sha256'], 'RESULT_CONFLICT', '같은 결과 ID의 내용이 다릅니다.', 409)
        return receipt(old), 200
    validate_dictionary(data['tags'])
    task = locked_task(task_id)
    require(task.item_id == item_id, 'TASK_NOT_FOUND', '결과의 작업 연결을 확인해 주세요.', 404)
    require(task.is_current and task.item.review_status == 'APPROVED' and task.status == 'RUNNING',
            'TASK_NOT_ACTIVE', '유효한 승인 작업이 아닙니다.', 409)
    require(task.lease_expires_at is not None and task.lease_expires_at > timezone.now()
            and hmac.compare_digest(task.lease_token_hash, hashlib.sha256(data['lease_token'].encode('utf-8')).hexdigest()),
            'LEASE_EXPIRED', '작업 임대가 만료됐거나 다른 작업자가 점유했습니다.', 409)
    require(task.source_ref == data['source_ref'] and (task.item.track_id or None) == data['track_id'],
            'SOURCE_MISMATCH', '원본과 곡 연결을 확인해 주세요.', 409)
    # Neither receipt nor tags write to the host Track/Review/Catalog tables.
    safe = {k: v for k, v in data.items() if k != 'lease_token'}
    stored = InferenceReceipt.objects.create(result_id=data['result_id'], task=task,
                                             payload_sha256=data['payload_sha256'], candidate=safe)
    task.status = 'RECEIVED_PENDING_REVIEW'
    task.lease_token_hash = ''
    task.lease_expires_at = None
    task.revision += 1
    task.save()
    SongItem.objects.filter(pk=task.item_id).update(pipeline_status='RECEIVED_PENDING_REVIEW')
    record('task', task.pk, 'worker', 'RESULT_RECEIVED', task.revision,
           result_id=stored.result_id, receipt_id=str(stored.receipt_id))
    return receipt(stored), 201


def receipt(row):
    return {'result_id': row.result_id, 'payload_sha256': row.payload_sha256,
            'receipt_id': str(row.receipt_id), 'status': 'RECEIVED_PENDING_REVIEW'}
