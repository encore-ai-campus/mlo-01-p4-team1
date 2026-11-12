import uuid
from django.db import transaction
from django.http import JsonResponse

from backend.venues.models.interaction import VenueInteraction
from backend.venues.models.outbox import VenueOutbox
from backend.venues.services.closure_service import report_venue_closure
from backend.venues.streaming.async_kafka_dispatcher import enqueue_behavior_event
from backend.venues.streaming.observability import emit_observation
from .access import consumer_post, ensure_request_id, parse_json_body


def _actor_id(request, body: dict, consumer) -> str:
    if consumer is not None:
        return f'platform:{consumer.pk}'
    return body.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'


@consumer_post
def venue_bookmark_api(request, venue_id: str, *, consumer):
    """
    POST /api/venues/<venue_id>/bookmark
    - 매장 찜하기 토글 API (플랫폼 소비자 또는 데모 게스트)
    - Two-Tower 딥러닝 취향 벡터 및 Outbox/관측 이벤트 기록
    """
    body, parse_error = parse_json_body(request, required=False)
    if parse_error:
        return parse_error

    user_id = _actor_id(request, body, consumer)
    request_id = ensure_request_id(request)
    event_id = str(uuid.uuid4())

    with transaction.atomic():
        existing = VenueInteraction.objects.filter(
            user_id=user_id,
            venue_id=venue_id,
            interaction_type='LIKE',
        ).first()

        if existing:
            existing.delete()
            is_bookmarked = False
            message = '찜 목록에서 삭제되었습니다.'
            event_type = 'BOOKMARK_REMOVED'
        else:
            VenueInteraction.objects.create(
                user_id=user_id,
                venue_id=venue_id,
                interaction_type='LIKE',
            )
            is_bookmarked = True
            message = '매장을 찜했습니다!'
            event_type = 'BOOKMARK_ADDED'

        VenueOutbox.objects.create(
            event_id=event_id,
            event_type=event_type,
            venue_id=venue_id,
            actor_id=str(user_id)[:64],
            request_id=request_id[:64],
            payload={'is_bookmarked': is_bookmarked},
        )

    if is_bookmarked:
        enqueue_behavior_event(
            user_id=user_id,
            venue_id=venue_id,
            behavior_type='BOOKMARK',
            extra_data={'weight': 3.0},
        )

    emit_observation(
        dataset='mlo.service',
        action='venue-bookmark',
        outcome='success',
        pipeline_name='venue-interactions',
        stage='bookmark',
        state='SUCCEEDED',
        business_event_id=event_id,
        request_id=request_id,
    )

    return JsonResponse({
        'success': True,
        'is_bookmarked': is_bookmarked,
        'message': message,
        'venue_id': venue_id,
    })


@consumer_post
def venue_review_api(request, venue_id: str, *, consumer):
    """
    POST /api/venues/<venue_id>/reviews
    - 날씨 스냅샷 한줄평 작성 API
    """
    body, parse_error = parse_json_body(request, required=True)
    if parse_error:
        return parse_error

    user_id = _actor_id(request, body, consumer)
    content = str(body.get('content', '') or '').strip()[:500]
    try:
        rating = int(body.get('rating', 5))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'message': '별점은 1~5 사이의 정수여야 합니다.'}, status=400)
    if rating < 1 or rating > 5:
        return JsonResponse({'success': False, 'message': '별점은 1~5점 범위여야 합니다.'}, status=400)

    weather = str(body.get('weather_condition', 'CLEAR') or 'CLEAR').strip().upper()[:20]
    try:
        temp = float(body.get('weather_temp', 20.0))
    except (TypeError, ValueError):
        temp = 20.0

    if not content:
        return JsonResponse({'success': False, 'message': '리뷰 내용을 입력해주세요.'}, status=400)

    request_id = ensure_request_id(request)
    event_id = str(uuid.uuid4())
    with transaction.atomic():
        review = VenueInteraction.objects.create(
            user_id=user_id,
            venue_id=venue_id,
            interaction_type='REVIEW',
            content=content,
            rating=rating,
            weather_condition=weather,
            weather_temp=temp,
        )
        VenueOutbox.objects.create(
            event_id=event_id,
            event_type='REVIEW_CREATED',
            venue_id=venue_id,
            actor_id=str(user_id)[:64],
            request_id=request_id[:64],
            payload={'rating': rating, 'weather_condition': weather},
        )

    emit_observation(
        dataset='mlo.service',
        action='venue-review-create',
        outcome='success',
        pipeline_name='venue-interactions',
        stage='review',
        state='SUCCEEDED',
        business_event_id=event_id,
        request_id=request_id,
    )

    return JsonResponse({
        'success': True,
        'message': '날씨 스냅샷 리뷰가 등록되었습니다!',
        'review_id': review.interaction_id,
        'created_at': review.created_at.isoformat(),
    })


@consumer_post
def venue_closure_report_api(request, venue_id: str, *, consumer):
    """
    POST /api/venues/<venue_id>/closure-report
    - 유저 폐업 제보 접수 API -> CLOSURE_REPORTED 즉시 전환
    """
    body, parse_error = parse_json_body(request, required=False)
    if parse_error:
        return parse_error

    user_id = _actor_id(request, body, consumer)
    reason = str(body.get('reason', '') or '').strip()[:500]
    request_id = ensure_request_id(request)

    overlay, _ = report_venue_closure(
        venue_id=venue_id,
        user_id=user_id,
        reason=reason,
        request_id=request_id,
    )

    return JsonResponse({
        'success': True,
        'message': '폐업 제보가 정상 접수되었습니다. 관리자 확인 후 처리됩니다.',
        'status': overlay.status,
        'status_label': overlay.get_status_display(),
        'report_count': overlay.closure_report_count,
    })


@consumer_post
def venue_interaction_generic_api(request, *, consumer):
    """
    POST /api/venues/interactions
    - 통합 유저 행동 로깅 및 Two-Tower 가중치 갱신 (비동기 큐 + Outbox 적재)
    """
    body, parse_error = parse_json_body(request, required=False)
    if parse_error:
        return parse_error

    user_id = _actor_id(request, body, consumer)
    venue_id = str(body.get('venue_id', '') or '').strip()[:64]
    action = str(body.get('interaction_type') or body.get('action') or 'CLICK').strip().upper()[:32]
    request_id = ensure_request_id(request)

    enqueued = enqueue_behavior_event(
        user_id=user_id,
        venue_id=venue_id,
        behavior_type=action,
        extra_data=body,
    )

    return JsonResponse({
        'success': True,
        'enqueued': enqueued,
        'message': '인터랙션 이벤트가 큐에 정상 등록되었습니다.',
    })
