def public_song_status(row):
    if row.review_status == 'PENDING':
        return '접수됨'
    if row.review_status == 'HOLD':
        return '보류'
    if row.review_status == 'REJECTED':
        return '반려'
    if row.catalog_version:
        return '추천 카탈로그 반영 완료'
    if row.pipeline_status == 'REGISTERED':
        return '등록 완료'
    if row.review_status == 'LINKED':
        return '기존 등록 곡 안내'
    if row.pipeline_status == 'RECEIVED_PENDING_REVIEW':
        return '태깅 결과 수신 · 검수 대기'
    return '처리 중'


def song_item(row, operator=False):
    result = {'id': str(row.pk), 'submission_id': str(row.submission_id), 'track_name': row.track_name,
              'artist_name': row.artist_name, 'youtube_url': row.youtube_url, 'review_status': row.review_status,
              'public_status': public_song_status(row), 'public_reason': row.public_reason, 'revision': row.revision}
    if operator:
        result.update(applicant_id=row.submission.account_id, pipeline_status=row.pipeline_status,
                      internal_note=row.internal_note, track_id=row.track_id, catalog_version=row.catalog_version)
    return result


def submission(row):
    return {'id': str(row.pk), 'venue_id': str(row.venue_id), 'created_at': row.created_at.isoformat(),
            'items': [song_item(item) for item in sorted(row.items.all(), key=lambda item: item.position)]}


def task(row, operator=False):
    result = {'id': str(row.pk), 'request_item_id': str(row.item_id), 'status': row.status,
              'revision': row.revision, 'source_ref': row.source_ref, 'attempts': row.attempts,
              'track_name': row.item.track_name, 'artist_name': row.item.artist_name,
              'lease_expires_at': row.lease_expires_at.isoformat() if row.lease_expires_at else None,
              'updated_at': row.updated_at.isoformat()}
    if operator:
        result['is_current'] = row.is_current
        receipt = getattr(row, 'receipt', None)
        result['result'] = ({'model_version': receipt.candidate['model_version'], 'tags': receipt.candidate['tags']}
                            if receipt else None)
        if receipt and 'metadata' in receipt.candidate:
            result['result']['metadata'] = receipt.candidate['metadata']
        result['registration'] = row.registration
        if row.status == 'LINKED':
            result['linked_track_id'] = row.item.track_id
    return result
