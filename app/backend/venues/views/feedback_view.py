import json
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from backend.venues.models.interaction import VenueInteraction
from backend.venues.services.closure_service import report_venue_closure
from backend.venues.streaming.async_kafka_dispatcher import enqueue_behavior_event


@csrf_exempt
@require_POST
def venue_bookmark_api(request, venue_id: str):
    """
    POST /api/venues/<venue_id>/bookmark
    - 매장 찜하기 토글 API (유저 또는 게스트)
    - Two-Tower 딥러닝 취향 벡터 즉각 갱신 (BOOKMARK w=3.0)
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'

    # 기존 찜 내역 확인
    existing = VenueInteraction.objects.filter(
        user_id=user_id,
        venue_id=venue_id,
        interaction_type='LIKE',
    ).first()

    if existing:
        existing.delete()
        is_bookmarked = False
        message = '찜 목록에서 삭제되었습니다.'
    else:
        VenueInteraction.objects.create(
            user_id=user_id,
            venue_id=venue_id,
            interaction_type='LIKE',
        )
        is_bookmarked = True
        message = '매장을 찜했습니다!'

        # Two-Tower 딥러닝 벡터 실시간 이동 (w=3.0)
        enqueue_behavior_event(
            user_id=user_id,
            venue_id=venue_id,
            behavior_type='BOOKMARK',
            extra_data={'weight': 3.0}
        )

    return JsonResponse({
        'success': True,
        'is_bookmarked': is_bookmarked,
        'message': message,
        'venue_id': venue_id,
    })


@csrf_exempt
@require_POST
def venue_review_api(request, venue_id: str):
    """
    POST /api/venues/<venue_id>/reviews
    - 날씨 스냅샷 한줄평 작성 API
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    content = body.get('content', '').strip()
    rating = int(body.get('rating', 5))
    weather = body.get('weather_condition', 'CLEAR')
    temp = float(body.get('weather_temp', 20.0))

    if not content:
        return JsonResponse({'success': False, 'message': '리뷰 내용을 입력해주세요.'}, status=400)

    review = VenueInteraction.objects.create(
        user_id=user_id,
        venue_id=venue_id,
        interaction_type='REVIEW',
        content=content,
        rating=rating,
        weather_condition=weather,
        weather_temp=temp,
    )

    return JsonResponse({
        'success': True,
        'message': '날씨 스냅샷 리뷰가 등록되었습니다!',
        'review_id': review.interaction_id,
        'created_at': review.created_at.isoformat(),
    })


@csrf_exempt
@require_POST
def venue_closure_report_api(request, venue_id: str):
    """
    POST /api/venues/<venue_id>/closure-report
    - 유저 폐업 제보 접수 API -> CLOSURE_REPORTED 즉시 전환
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    reason = body.get('reason', '').strip()

    overlay, _ = report_venue_closure(
        venue_id=venue_id,
        user_id=user_id,
        reason=reason,
    )

    return JsonResponse({
        'success': True,
        'message': '폐업 제보가 정상 접수되었습니다. 관리자 확인 후 처리됩니다.',
        'status': overlay.status,
        'status_label': overlay.get_status_display(),
        'report_count': overlay.closure_report_count,
    })


@csrf_exempt
@require_POST
def venue_interaction_generic_api(request):
    """
    POST /api/venues/interactions
    - 통합 유저 행동 로깅 및 Two-Tower 가중치 갱신 (비동기 0.001초 응답)
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    venue_id = body.get('venue_id', '')
    action = body.get('interaction_type') or body.get('action') or 'CLICK'

    enqueue_behavior_event(
        user_id=user_id,
        venue_id=venue_id,
        behavior_type=action,
        extra_data=body
    )

    return JsonResponse({
        'success': True,
        'message': '인터랙션 이벤트가 큐에 정상 등록되었습니다.',
    })
