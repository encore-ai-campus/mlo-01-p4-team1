import datetime
import uuid
from typing import Any, Dict, List, Optional, Tuple
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from backend.venues.models.event import (
    VenueEvent,
    VenueEventParticipation,
    OwnerEventTrustScore,
    UserNotification,
)
from backend.venues.models.interaction import VenueInteraction
from backend.venues.streaming.async_kafka_dispatcher import enqueue_behavior_event


def create_owner_event(
    venue_id: str,
    owner_id: str,
    title: str,
    description: str,
    badge_text: str,
    discount_info: str,
    total_quantity: int,
    start_at: datetime.datetime,
    end_at: datetime.datetime,
) -> VenueEvent:
    """점주 실시간 이벤트 생성 및 찜한 유저들에게 알림 발송"""
    event = VenueEvent.objects.create(
        venue_id=venue_id,
        owner_id=owner_id,
        title=title,
        description=description,
        badge_text=badge_text,
        discount_info=discount_info,
        total_quantity=total_quantity,
        remaining_quantity=total_quantity,
        start_at=start_at,
        end_at=end_at,
        is_active=True,
    )

    # 점주 신뢰도 테이블 누적 카운트
    trust, _ = OwnerEventTrustScore.objects.get_or_create(
        owner_id=owner_id,
        defaults={'venue_id': venue_id, 'trust_score': 100.0}
    )
    trust.total_events_hosted += 1
    trust.save()

    # 해당 매장을 찜한 유저들에게 BOOKMARK_EVENT_START 알림 생성
    bookmarked_users = VenueInteraction.objects.filter(
        venue_id=venue_id,
        interaction_type__in=['LIKE', 'BOOKMARK']
    ).values_list('user_id', flat=True).distinct()

    notifications = []
    for uid in bookmarked_users:
        notifications.append(UserNotification(
            user_id=uid,
            title=f"❤️ 찜한 매장 새 이벤트: {title}",
            message=f"관심 매장에서 새 이벤트가 시작되었어요! ({discount_info})",
            link_url=f"/venues?event_id={event.event_id}&venue_id={venue_id}",
        ))
    if notifications:
        UserNotification.objects.bulk_create(notifications)

    return event


def register_event_intent(
    event_id: int,
    user_id: str,
) -> Dict[str, Any]:
    """
    유저 [참여할게요] 클릭 처리 (PA 기획안):
    - 재고(remaining_quantity)는 차감하지 않는다!
    - VenueEventParticipation에 'INTENDED' 상태로 기록 (당일 1회 제한 확인)
    - 점주에게 실시간 예상 방문 수요 알림 전송
    - Two-Tower DL에 weight=5.0 전달 (비동기 0.001초 응답)
    """
    if not user_id:
        return {'success': False, 'code': 401, 'message': '로그인이 필요한 기능입니다.'}

    now = timezone.now()
    today = now.date()

    try:
        event = VenueEvent.objects.get(event_id=event_id)
    except VenueEvent.DoesNotExist:
        return {'success': False, 'code': 404, 'message': '존재하지 않는 이벤트입니다.'}

    if not event.is_active or event.is_ended:
        return {'success': False, 'code': 400, 'message': '이미 종료된 이벤트입니다.'}

    # 당일 중복 참여 여부 확인
    existing = VenueEventParticipation.objects.filter(
        event=event,
        user_id=user_id,
        participation_date=today,
    ).first()

    if existing:
        return {
            'success': False,
            'code': 409,
            'error': 'ALREADY_PARTICIPATED_TODAY',
            'message': '오늘 이미 참여 신청한 이벤트입니다.',
            'remaining_quantity': event.remaining_quantity,
            'intended_count': event.intended_visitors_count,
        }

    # 참여 의사 등록 (재고 차감 없음!)
    qr_code = f"QR_{event.event_id}_{user_id}_{uuid.uuid4().hex[:8]}"
    participation = VenueEventParticipation.objects.create(
        event=event,
        user_id=user_id,
        status='INTENDED',
        qr_auth_code=qr_code,
    )

    # Two-Tower 딥러닝 취향 벡터 이동 (논블로킹 스트리밍)
    enqueue_behavior_event(
        user_id=user_id,
        venue_id=event.venue_id,
        behavior_type='EVENT_INTENT',
        extra_data={'event_id': event_id, 'weight': 5.0}
    )

    # 점주에게 수요 알림 발송
    intended_cnt = event.intended_visitors_count
    UserNotification.objects.create(
        user_id=event.owner_id,
        title=f"🔥 [방문 예정] {event.title}",
        message=f"새 유저가 방문을 약속했어요! 현재 방문 예정: {intended_cnt}명 (남은 재고: {event.remaining_quantity}개)",
        link_url="/platform/business",
    )

    return {
        'success': True,
        'code': 200,
        'message': '참여 신청이 완료되었습니다! 매장 방문 시 현장 QR 인증을 진행해주세요.',
        'qr_auth_code': qr_code,
        'remaining_quantity': event.remaining_quantity,
        'intended_count': intended_cnt,
    }


def verify_onsite_qr_and_deduct_stock(
    event_id: int,
    user_id: str,
    qr_auth_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    현장 QR 인증 및 실시간 재고 차감 (PA 기획안 핵심):
    - select_for_update() 행 잠금으로 remaining_quantity -1 원자적 차감
    - 계정당 동일 이벤트 1일 1회 제한 검사 (위반 시 409 ALREADY_PARTICIPATED_TODAY)
    - 재고 0 소진 시 즉시 마감 및 미인증 참여자 마감 알림 전송
    - Two-Tower DL에 weight=6.0 전달
    """
    if not user_id:
        return {'success': False, 'code': 401, 'message': '로그인이 필요한 기능입니다.'}

    now = timezone.now()
    today = now.date()

    with transaction.atomic():
        # select_for_update()로 동시성 행 잠금
        try:
            event = VenueEvent.objects.select_for_update().get(event_id=event_id)
        except VenueEvent.DoesNotExist:
            return {'success': False, 'code': 404, 'message': '존재하지 않는 이벤트입니다.'}

        # 1. 1일 1회 완료 이력 확인
        already_verified = VenueEventParticipation.objects.filter(
            event=event,
            user_id=user_id,
            participation_date=today,
            status='VERIFIED',
        ).exists()

        if already_verified:
            return {
                'success': False,
                'code': 409,
                'error': 'ALREADY_PARTICIPATED_TODAY',
                'message': '해당 이벤트는 1일 1회만 현장 인증이 가능합니다.',
            }

        # 2. 이벤트 활성 및 재고 확인
        if not event.is_active or event.remaining_quantity <= 0:
            return {'success': False, 'code': 410, 'message': '이벤트 재고가 모두 소진되었거나 종료되었습니다.'}

        # 3. 재고 1 차감 (원자적)
        event.remaining_quantity -= 1
        is_sold_out = (event.remaining_quantity == 0)
        if is_sold_out:
            event.is_active = False
            event.end_at = now

        event.save()

        # 4. 참여 기록 갱신 또는 신규 생성
        participation = VenueEventParticipation.objects.filter(
            event=event,
            user_id=user_id,
            participation_date=today,
        ).first()

        if participation:
            participation.status = 'VERIFIED'
            participation.verified_at = now
            if qr_auth_code:
                participation.qr_auth_code = qr_auth_code
            participation.save()
        else:
            participation = VenueEventParticipation.objects.create(
                event=event,
                user_id=user_id,
                status='VERIFIED',
                verified_at=now,
                qr_auth_code=qr_auth_code or f"QR_{uuid.uuid4().hex[:8]}",
            )

        # 5. 점주 신뢰도 점수 및 실적 증가
        trust = OwnerEventTrustScore.objects.filter(owner_id=event.owner_id).first()
        if trust:
            trust.successful_verifications += 1
            trust.save()

        # 6. 유저에게 인증 성공 알림
        UserNotification.objects.create(
            user_id=user_id,
            title=f"🎉 [QR 인증 완료] {event.title}",
            message=f"현장 QR 인증이 완료되었습니다! 혜택({event.discount_info})을 즐겨보세요.",
            link_url=f"/venues?venue_id={event.venue_id}",
        )

        # 7. 재고 소진 시 대기자들에게 마감 알림
        if is_sold_out:
            waiting_users = VenueEventParticipation.objects.filter(
                event=event,
                status='INTENDED',
            ).exclude(user_id=user_id).values_list('user_id', flat=True)

            soldout_notis = [
                UserNotification(
                    user_id=uid,
                    title=f"⚠️ [이벤트 마감] {event.title}",
                    message="참여 신청하셨던 이벤트의 수량이 모두 소진되어 조기 마감되었습니다.",
                    link_url=f"/venues?venue_id={event.venue_id}",
                )
                for uid in waiting_users
            ]
            if soldout_notis:
                UserNotification.objects.bulk_create(soldout_notis)

    # 8. Two-Tower DL에 QR 인증 완료 가중치(w=6.0) 전달 (트랜잭션 바깥에서 논블로킹)
    enqueue_behavior_event(
        user_id=user_id,
        venue_id=event.venue_id,
        behavior_type='EVENT_QR_VERIFIED',
        extra_data={'event_id': event_id, 'weight': 6.0}
    )

    return {
        'success': True,
        'code': 200,
        'message': '현장 QR 인증 및 혜택 적용이 완료되었습니다!',
        'remaining_quantity': event.remaining_quantity,
        'is_sold_out': is_sold_out,
    }


def _serialize_visible_event(ev: Any, now: datetime.datetime) -> Optional[Dict[str, Any]]:
    """이벤트 1건을 노출용 dict 로 변환 (종료 후 5분 초과 시 None)"""
    is_ended = (now > ev.end_at)
    grace_period = (ev.end_at < now <= (ev.end_at + datetime.timedelta(minutes=5)))

    if grace_period:
        display_badge = '이벤트가 종료되었어요'
    elif is_ended:
        return None
    else:
        display_badge = ev.badge_text

    intended_count = getattr(ev, '_intended_count', None)
    if intended_count is None:
        intended_count = ev.intended_visitors_count

    return {
        'event_id': ev.event_id,
        'venue_id': ev.venue_id,
        'title': ev.title,
        'description': ev.description,
        'badge_text': display_badge,
        'discount_info': ev.discount_info,
        'total_quantity': ev.total_quantity,
        'remaining_quantity': ev.remaining_quantity,
        'intended_count': intended_count,
        'start_at': ev.start_at.isoformat(),
        'end_at': ev.end_at.isoformat(),
        'is_ended': is_ended,
        'is_ended_grace_period': grace_period,
        'status_label': f"🔥 남은 수량: {ev.remaining_quantity}개 (현재 방문 예정: {intended_count}명)" if not is_ended else "종료된 이벤트",
    }


def get_visible_events_for_venues(
    venue_ids: List[str],
    as_of: Optional[datetime.datetime] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    여러 매장의 현재 유효 이벤트를 일괄 조회 (목록 랭킹의 매장당 쿼리 N+1 제거용).
    결과는 이벤트가 있는 매장만 키로 포함하며, 규칙은 get_visible_events_for_venue 와 동일.
    """
    now = as_of or timezone.now()
    five_min_ago = now - datetime.timedelta(minutes=5)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    unique_ids = list(dict.fromkeys(venue_ids))
    # SQLite 바인딩 변수 한도를 넘지 않도록 분할 조회 + 참여자 수 COUNT 일괄 집계
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start:start + 500]
        events = (
            VenueEvent.objects.filter(
                venue_id__in=chunk,
                end_at__gte=five_min_ago,
            )
            .annotate(
                _intended_count=Count(
                    'participations',
                    filter=Q(participations__status='INTENDED'),
                )
            )
            .order_by('-created_at')
        )
        for ev in events:
            item = _serialize_visible_event(ev, now)
            if item is not None:
                grouped.setdefault(ev.venue_id, []).append(item)
    return grouped


def get_visible_events_for_venue(
    venue_id: str,
    as_of: Optional[datetime.datetime] = None,
    prefetched: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """
    특정 매장의 현재 유효 이벤트 조회:
    - 종료 후 5분 이내인 이벤트는 '이벤트가 종료되었어요' 배지 유지
    - 5분 경과 시 자동 숨김
    - prefetched(get_visible_events_for_venues 결과)가 주어지면 추가 쿼리 없이 사용
    """
    if prefetched is not None:
        return list(prefetched.get(venue_id, []))

    now = as_of or timezone.now()
    five_min_ago = now - datetime.timedelta(minutes=5)

    events = (
        VenueEvent.objects.filter(
            venue_id=venue_id,
            end_at__gte=five_min_ago,
        )
        .annotate(
            _intended_count=Count(
                'participations',
                filter=Q(participations__status='INTENDED'),
            )
        )
        .order_by('-created_at')
    )

    results = []
    for ev in events:
        item = _serialize_visible_event(ev, now)
        if item is not None:
            results.append(item)

    return results


def get_active_events_in_gu(gu_name: str, limit: int = 5) -> List[Dict[str, Any]]:
    """해당 자치구에서 진행 중인 실시간 이벤트 상단 배너용 목록"""
    now = timezone.now()
    events = VenueEvent.objects.filter(
        is_active=True,
        start_at__lte=now,
        end_at__gte=now,
        remaining_quantity__gt=0,
    ).order_by('-created_at')[:limit]

    return [
        {
            'event_id': ev.event_id,
            'venue_id': ev.venue_id,
            'title': ev.title,
            'badge_text': ev.badge_text,
            'discount_info': ev.discount_info,
            'remaining_quantity': ev.remaining_quantity,
            'intended_count': ev.intended_visitors_count,
        }
        for ev in events
    ]


def check_and_send_nearby_event_promo(user_id: str, gu_name: str) -> bool:
    """
    유저가 동네 조회 시 당일 NEARBY_EVENT_PROMO 알림이 3회 미만이고
    해당 동네에 진행 중인 이벤트가 있으면 자동 프로모션 알림 1회 생성
    """
    if not user_id:
        return False

    today = timezone.now().date()
    # 당일 프로모 알림 발송 횟수 검사
    promo_count = UserNotification.objects.filter(
        user_id=user_id,
        title__contains='[주변 이벤트]',
        created_at__date=today,
    ).count()

    if promo_count >= 3:
        return False

    # 해당 구의 진행 중 이벤트 확인
    active_events = get_active_events_in_gu(gu_name, limit=1)
    if not active_events:
        return False

    top_ev = active_events[0]
    UserNotification.objects.create(
        user_id=user_id,
        title=f"📍 [{gu_name} 주변 이벤트] {top_ev['title']}",
        message=f"지금 {gu_name}에서 {top_ev['discount_info']} 혜택이 진행 중이에요! (남은 수량: {top_ev['remaining_quantity']}개)",
        link_url=f"/venues?gu={gu_name}&event_id={top_ev['event_id']}",
    )
    return True
