from django.db import transaction

from . import serialization as out
from .catalog_port import require_confirmed_existing_track
from .errors import require
from .models import PipelineTask
from .pipeline_services import locked_review_result
from .song_services import check_queue_source, queue_input
from .transactions import record
from .validation import expected_revision, shape, text, youtube_url


@transaction.atomic
def recover(actor, task_id, data):
    require('review_songs' in actor.capabilities, 'PERMISSION_DENIED', '곡 검수 권한이 필요합니다.', 403)
    base_fields = ['decision', 'expected_revision', 'reason']
    shape(data, [*base_fields, 'track_id', 'youtube_url'], base_fields)
    decision = data['decision']
    extra = {'REJECT': [], 'LINK_EXISTING': ['track_id'], 'REQUEUE': ['youtube_url']}
    require(type(decision) is str and decision in extra,
            'INVALID_INPUT', '결과 처리 결정을 확인해 주세요.')
    fields = [*base_fields, *extra[decision]]
    shape(data, fields, fields)
    reason = text(data['reason'], '공개 사유', 1000)
    task, receipt = locked_review_result(task_id, expected_revision(data['expected_revision']))
    item = task.item
    source = youtube_url(item.youtube_url)
    require(source and source == task.source_ref == receipt.candidate['source_ref'],
            'SOURCE_MISMATCH', '승인한 영상과 추론 결과의 연결을 확인해 주세요.', 409)

    if decision == 'REQUEUE':
        corrected = youtube_url(data['youtube_url'])
        require(bool(corrected), 'VIDEO_REQUIRED', '곡과 일치하는 YouTube 링크를 확인해 입력해 주세요.')
        require(corrected != source, 'SOURCE_UNCHANGED', '기존과 다른 영상 주소를 입력해 주세요.', 422)
        check_queue_source(item, corrected)
        item.youtube_url = corrected
        item.pipeline_status = 'READY'
        task.status = 'SUPERSEDED'
        task.is_current = False
    elif decision == 'LINK_EXISTING':
        track_id = text(data['track_id'], '곡 ID', 128)
        require_confirmed_existing_track(track_id, source)
        item.track_id = track_id
        item.review_status = item.pipeline_status = task.status = 'LINKED'
    else:
        item.review_status = item.pipeline_status = task.status = 'REJECTED'

    item.public_reason = reason
    item.reviewer_id = actor.account_id
    item.revision += 1
    item.save(update_fields=['youtube_url', 'track_id', 'review_status', 'pipeline_status', 'public_reason',
                             'reviewer_id', 'revision', 'updated_at'])
    task.revision += 1
    task.save(update_fields=['status', 'is_current', 'revision', 'updated_at'])
    current_task = task
    audit = {'reason': reason, 'receipt_id': str(receipt.receipt_id)}
    if decision == 'REQUEUE':
        current_task = PipelineTask.objects.create(
            item=item, status='READY', source_ref=corrected, input_payload=queue_input(item, corrected),
        )
        audit['new_task_id'] = str(current_task.pk)
        record('task', current_task.pk, actor.account_id, 'INPUT_PREPARED', current_task.revision,
               previous_task_id=str(task.pk))
    elif decision == 'LINK_EXISTING':
        audit['track_id'] = item.track_id
    record('task', task.pk, actor.account_id, task.status, task.revision, **audit)
    record('song_item', item.pk, actor.account_id, decision, item.revision, task_id=str(task.pk), **audit)
    return out.task(current_task, operator=True), 200
