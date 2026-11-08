import json
import datetime
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from backend.venues.models.event import (
    VenueEvent,
    VenueEventParticipation,
    UserNotification,
)
from backend.venues.models.interaction import VenueInteraction
from backend.venues.repositories.csv_lazy_repo import get_venue_repo
from backend.venues.services.event_service import (
    create_owner_event,
    register_event_intent,
    verify_onsite_qr_and_deduct_stock,
    get_active_events_in_gu,
)


@require_GET
def active_events_banner_api(request):
    """
    GET /api/venues/events/active
    - 상단 파트너 매장 & 실시간 이벤트 배너용 활성 이벤트 목록
    """
    gu = request.GET.get('gu', '강남구')
    events = get_active_events_in_gu(gu, limit=10)
    return JsonResponse({
        'success': True,
        'gu': gu,
        'events': events,
    })


@csrf_exempt
@require_POST
def owner_create_event_api(request, venue_id: str):
    """
    POST /api/owner/venues/<venue_id>/events
    - 점주 실시간 타임 이벤트 발행 API
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)

    owner_id = body.get('owner_id') or 'owner_demo'
    title = body.get('title', '오늘의 깜짝 혜택').strip()
    description = body.get('description', '')
    badge_text = body.get('badge_text', '오늘의 이벤트')
    discount_info = body.get('discount_info', '1+1 혜택')
    total_quantity = int(body.get('total_quantity', 10))
    duration_hours = int(body.get('duration_hours', 2))

    now = timezone.now()
    start_at = now
    end_at = now + datetime.timedelta(hours=duration_hours)

    event = create_owner_event(
        venue_id=venue_id,
        owner_id=owner_id,
        title=title,
        description=description,
        badge_text=badge_text,
        discount_info=discount_info,
        total_quantity=total_quantity,
        start_at=start_at,
        end_at=end_at,
    )

    return JsonResponse({
        'success': True,
        'message': '점주 이벤트가 성공적으로 등록 및 시작되었습니다!',
        'event_id': event.event_id,
        'remaining_quantity': event.remaining_quantity,
        'start_at': event.start_at.isoformat(),
        'end_at': event.end_at.isoformat(),
    })


@csrf_exempt
@require_POST
def owner_stop_event_api(request, event_id: int):
    """
    POST /api/owner/venues/events/<event_id>/stop
    - 점주 이벤트 조기 종료 API
    """
    try:
        event = VenueEvent.objects.get(event_id=event_id)
    except VenueEvent.DoesNotExist:
        return JsonResponse({'success': False, 'message': '이벤트를 찾을 수 없습니다.'}, status=404)

    event.is_active = False
    event.end_at = timezone.now()
    event.save()

    # [PA 기획안 반영] 방문 전(INTENDED) 대기자 전원에게 조기 종료 알림 즉시 발송
    waiting_users = VenueEventParticipation.objects.filter(
        event=event,
        status='INTENDED',
    ).values_list('user_id', flat=True)

    stop_notis = [
        UserNotification(
            user_id=uid,
            title=f"😢 [이벤트 조기 종료] {event.title}",
            message="참여 예정이셨던 매장 이벤트가 점주 사정(품절 등)으로 조기 종료되었습니다.",
            link_url=f"/venues?venue_id={event.venue_id}",
        )
        for uid in waiting_users
    ]
    if stop_notis:
        UserNotification.objects.bulk_create(stop_notis)

    return JsonResponse({
        'success': True,
        'message': '이벤트가 조기 종료되었습니다.',
        'event_id': event_id,
    })


@csrf_exempt
@require_POST
def event_intent_api(request, event_id: int):
    """
    POST /api/venues/events/<event_id>/intent
    - 유저 [참여할게요] 클릭 API (재고 미차감, 방문예정 +1)
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest')
    if not user_id:
        return JsonResponse({
            'success': False,
            'message': '로그인이 필요한 기능입니다.',
        }, status=401)

    result = register_event_intent(event_id=event_id, user_id=user_id)
    status_code = result.get('code', 200)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
def event_verify_qr_api(request, event_id: int):
    """
    POST /api/venues/events/<event_id>/verify-qr
    - 현장 방문 QR 스캔 / 인증 코드 / 시연용 즉시 인증 API (재고 -1 차감)
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    user_id = body.get('user_id') or request.COOKIES.get('cafe_guest')
    if not user_id:
        return JsonResponse({
            'success': False,
            'message': '로그인이 필요한 기능입니다.',
        }, status=401)

    qr_code = body.get('qr_auth_code')
    result = verify_onsite_qr_and_deduct_stock(
        event_id=event_id,
        user_id=user_id,
        qr_auth_code=qr_code,
    )
    status_code = result.get('code', 200)
    return JsonResponse(result, status=status_code)


@csrf_exempt
@require_POST
def event_report_false_api(request, event_id: int):
    """
    POST /api/venues/events/<event_id>/report-false
    - 허위 이벤트 신고 접수
    """
    try:
        event = VenueEvent.objects.get(event_id=event_id)
    except VenueEvent.DoesNotExist:
        return JsonResponse({'success': False, 'message': '이벤트를 찾을 수 없습니다.'}, status=404)

    return JsonResponse({
        'success': True,
        'message': '허위 이벤트 신고가 접수되었습니다. 관리자 검토 후 조치됩니다.',
        'event_id': event_id,
    })


@require_GET
def venue_my_summary_api(request):
    """
    GET /api/venues/my/summary
    - 마이페이지 통합 데이터 조회 (찜 매장, 내 리뷰, 참여 이벤트, 알림 목록)
    """
    user_id = request.GET.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    repo = get_venue_repo()

    # 1. 찜한 매장 목록
    bookmarks = VenueInteraction.objects.filter(
        user_id=user_id,
        interaction_type='LIKE',
    ).order_by('-created_at')[:20]

    bookmarked_venues = []
    for b in bookmarks:
        v = repo.get_venue_by_id(b.venue_id)
        if v:
            bookmarked_venues.append({
                'venue_id': v['venue_id'],
                'venue_name': v['venue_name'],
                'gu_name': v['gu_name'],
                'category': v['category'],
                'bookmarked_at': b.created_at.isoformat(),
            })

    # 2. 내가 쓴 리뷰 목록
    reviews = VenueInteraction.objects.filter(
        user_id=user_id,
        interaction_type='REVIEW',
    ).order_by('-created_at')[:20]

    review_list = []
    for r in reviews:
        v = repo.get_venue_by_id(r.venue_id)
        review_list.append({
            'review_id': r.interaction_id,
            'venue_id': r.venue_id,
            'venue_name': v['venue_name'] if v else '매장',
            'content': r.content,
            'rating': r.rating,
            'weather_condition': r.weather_condition,
            'weather_temp': r.weather_temp,
            'created_at': r.created_at.isoformat(),
        })

    # 3. 오늘 참여 및 인증한 이벤트 목록
    participations = VenueEventParticipation.objects.filter(
        user_id=user_id,
    ).select_related('event').order_by('-created_at')[:20]

    part_list = []
    for p in participations:
        part_list.append({
            'participation_id': p.participation_id,
            'event_id': p.event_id,
            'title': p.event.title,
            'status': p.status,
            'status_label': p.get_status_display(),
            'qr_auth_code': p.qr_auth_code,
            'verified_at': p.verified_at.isoformat() if p.verified_at else None,
            'created_at': p.created_at.isoformat(),
        })

    # 4. 실시간 알림 목록
    notis_qs = UserNotification.objects.filter(user_id=user_id)
    unread_count = notis_qs.filter(is_read=False).count()
    notifications = notis_qs.order_by('-created_at')[:20]

    noti_list = [
        {
            'notification_id': n.notification_id,
            'title': n.title,
            'message': n.message,
            'link_url': n.link_url,
            'is_read': n.is_read,
            'created_at': n.created_at.isoformat(),
        }
        for n in notifications
    ]

    return JsonResponse({
        'success': True,
        'user_id': user_id,
        'summary': {
            'bookmarked_venues': bookmarked_venues,
            'reviews': review_list,
            'participations': part_list,
            'notifications': noti_list,
            'unread_notifications_count': unread_count,
        }
    })


@require_GET
def venue_notifications_api(request):
    """
    GET /api/venues/notifications
    - 유저 실시간 알림 목록 조회 API
    """
    user_id = request.GET.get('user_id') or request.COOKIES.get('cafe_guest') or 'guest_anonymous'
    notis_qs = UserNotification.objects.filter(user_id=user_id)
    unread_count = notis_qs.filter(is_read=False).count()
    notifications = notis_qs.order_by('-created_at')[:30]

    return JsonResponse({
        'success': True,
        'unread_count': unread_count,
        'notifications': [
            {
                'notification_id': n.notification_id,
                'title': n.title,
                'message': n.message,
                'link_url': n.link_url,
                'is_read': n.is_read,
                'created_at': n.created_at.isoformat(),
            }
            for n in notifications
        ],
    })
