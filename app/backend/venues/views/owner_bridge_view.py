import hashlib
import json
import uuid
from django.db import transaction
from django.http import JsonResponse

from backend.venues.models.bridge import VenueOwnerBridge
from backend.venues.models.catalog import VenueCatalogRecord
from backend.venues.models.outbox import VenueOutbox
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.repositories.csv_lazy_repo import METRO_CENTER_COORDS, get_venue_repo
from backend.venues.streaming.observability import emit_observation
from .access import business_post, ensure_request_id, parse_json_body


@business_post
def owner_claim_venue_api(request, *, business_user):
    """
    POST /api/owner/venues/claim
    - 사장님이 기존 CSV 매장에 대해 소유권 인증/연결
    """
    body, parse_error = parse_json_body(request, required=True)
    if parse_error:
        return parse_error

    owner_id = f'platform:{business_user.pk}' if business_user is not None else body.get('owner_id')
    venue_id = str(body.get('venue_id', '') or '').strip()
    biz_num = str(body.get('business_number', '') or '').strip()[:30]

    if not owner_id or not venue_id:
        return JsonResponse({'success': False, 'message': 'owner_id와 venue_id는 필수입니다.'}, status=400)

    if VenueStatusOverlay.objects.filter(venue_id=venue_id, status='CLOSED_HIDDEN').exists():
        return JsonResponse({'success': False, 'code': 'VENUE_UNAVAILABLE', 'message': '폐업 확정된 매장에는 연결할 수 없습니다.'}, status=409)

    repo = get_venue_repo()
    venue = repo.get_venue_by_id(venue_id)
    if not venue:
        return JsonResponse({'success': False, 'message': '해당 매장을 찾을 수 없습니다.'}, status=404)

    canonical_id = venue['venue_id']
    if canonical_id != venue_id and VenueStatusOverlay.objects.filter(venue_id=canonical_id, status='CLOSED_HIDDEN').exists():
        return JsonResponse({'success': False, 'code': 'VENUE_UNAVAILABLE', 'message': '폐업 확정된 매장에는 연결할 수 없습니다.'}, status=409)

    request_id = ensure_request_id(request)
    with transaction.atomic():
        bridge, created = VenueOwnerBridge.objects.get_or_create(
            venue_id=canonical_id,
            defaults={
                'owner_id': owner_id,
                'venue_name': venue['venue_name'],
                'gu_name': venue['gu_name'],
                'road_address': venue['road_address'],
                'category': venue['category'],
                'business_number': biz_num,
                'is_verified': True,
            },
        )

        if not created:
            if business_user is not None and bridge.owner_id != owner_id:
                return JsonResponse({'success': False, 'code': 'WORKSPACE_ALREADY_LINKED', 'message': '이미 다른 점주가 연결한 매장입니다.'}, status=409)
            bridge.owner_id = owner_id
            bridge.business_number = biz_num
            bridge.is_verified = True
            bridge.save()

        VenueOutbox.objects.create(
            event_id=str(uuid.uuid4()),
            event_type='OWNER_VENUE_CLAIMED',
            venue_id=canonical_id,
            actor_id=str(owner_id)[:64],
            request_id=request_id[:64],
            payload={'category': venue.get('category', 'CAFE'), 'gu_name': venue.get('gu_name', '')},
        )

    repo.invalidate_gu(venue.get('gu_name', ''))
    emit_observation(
        dataset='mlo.service',
        action='venue-owner-bridge-claim',
        outcome='success',
        pipeline_name='venue-owner-bridge',
        stage='claim',
        state='COMMITTED',
        request_id=request_id,
    )

    return JsonResponse({
        'success': True,
        'message': f"'{venue['venue_name']}' 매장의 점주 인증이 완료되었습니다.",
        'venue_id': canonical_id,
    })


def _safe_coord(raw_val, default_val: float) -> float:
    try:
        parsed = float(raw_val)
        return parsed if -180.0 <= parsed <= 180.0 else default_val
    except (TypeError, ValueError):
        return default_val


def _safe_positive_int(raw_val, default_val: int) -> int:
    try:
        parsed = int(raw_val)
        return parsed if parsed > 0 else default_val
    except (TypeError, ValueError):
        return default_val


@business_post
def owner_register_venue_api(request, *, business_user):
    """
    POST /api/owner/venues/register
    - 사장님 신규 매장 직접 등록 (VenueOwnerBridge + VenueCatalogRecord + Outbox 동시 반영 -> 즉시 /venues 노출)
    """
    body, parse_error = parse_json_body(request, required=True)
    if parse_error:
        return parse_error

    owner_id = f'platform:{business_user.pk}' if business_user is not None else body.get('owner_id')
    venue_name = str(body.get('venue_name', '') or '').strip()[:200]
    gu_name = str(body.get('gu_name', '강남구') or '강남구').strip()[:50]
    dong_name = str(body.get('dong_name', '') or '').strip()[:80]
    road_address = str(body.get('road_address', '') or '').strip()[:255]
    category = str(body.get('category', 'CAFE') or 'CAFE').strip().upper()
    if category not in ('CAFE', 'PUB'):
        category = 'CAFE'
    sub_category = str(body.get('sub_category') or ('카페' if category == 'CAFE' else '주점')).strip()[:100]
    biz_num = str(body.get('business_number', '') or '').strip()[:30]
    open_hours = str(body.get('open_hours', '매일 10:00-22:00') or '매일 10:00-22:00').strip()[:120]
    phone = str(body.get('phone', '') or '').strip()[:40]
    rec_style = str(body.get('recommendation_style', '매장 점주 추천 공간 맞춤 음악') or '').strip()[:120]

    if not owner_id or not venue_name:
        return JsonResponse({'success': False, 'message': 'owner_id와 venue_name은 필수입니다.'}, status=400)

    default_lat, default_lng = METRO_CENTER_COORDS.get(gu_name, (37.5172, 127.0286))
    latitude = _safe_coord(body.get('latitude'), default_lat)
    longitude = _safe_coord(body.get('longitude'), default_lng)
    default_price = 9000 if category == 'CAFE' else 25000
    avg_price = _safe_positive_int(body.get('avg_price'), default_price)
    default_tier = 1 if avg_price < 10000 else 2 if avg_price < 20000 else 3 if avg_price < 30000 else 4 if avg_price < 50000 else 5
    price_tier = max(1, min(5, _safe_positive_int(body.get('price_tier'), default_tier)))
    raw_moods = body.get('mood_tags')
    mood_tags = [str(tag).strip()[:30] for tag in raw_moods if str(tag).strip()] if isinstance(raw_moods, list) else ['점주인증', '직접등록']
    raw_menus = body.get('menus')
    menus_payload = raw_menus if isinstance(raw_menus, list) else []

    new_venue_id = f"custom_venue_{uuid.uuid4().hex[:10]}"
    request_id = ensure_request_id(request)
    venue_payload = {
        'venue_id': new_venue_id,
        'venue_name': venue_name,
        'gu_name': gu_name,
        'dong_name': dong_name,
        'road_address': road_address,
        'category': category,
        'sub_category': sub_category,
        'store_type': 'LOCAL',
        'brand_name': '개인매장',
        'price_tier': price_tier,
        'avg_price': avg_price,
        'primary_weather_affinity': 'ALL',
        'open_hours': open_hours,
        'phone': phone,
        'mood_tags': mood_tags,
        'recommendation_style': rec_style,
        'is_partner': True,
        'rating': 0.0,
        'review_count': 0,
        'latitude': latitude,
        'longitude': longitude,
    }
    source_sha = hashlib.sha256(json.dumps(venue_payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    with transaction.atomic():
        VenueOwnerBridge.objects.create(
            owner_id=owner_id,
            venue_id=new_venue_id,
            venue_name=venue_name,
            gu_name=gu_name,
            road_address=road_address,
            category=category,
            business_number=biz_num,
            is_verified=True,
        )
        VenueCatalogRecord.objects.create(
            venue_id=new_venue_id,
            dataset_version='owner_direct',
            venue_name=venue_name,
            gu_name=gu_name,
            dong_name=dong_name,
            road_address=road_address,
            category=category,
            sub_category=sub_category,
            store_type='LOCAL',
            brand_name='개인매장',
            price_tier=price_tier,
            avg_price=avg_price,
            latitude=latitude,
            longitude=longitude,
            is_partner=True,
            is_open_now=True,
            primary_weather_affinity='ALL',
            venue_payload=venue_payload,
            menus_payload=menus_payload,
            source_sha256=source_sha,
        )
        VenueOutbox.objects.create(
            event_id=str(uuid.uuid4()),
            event_type='OWNER_VENUE_REGISTERED',
            venue_id=new_venue_id,
            actor_id=str(owner_id)[:64],
            request_id=request_id[:64],
            payload={'gu_name': gu_name, 'category': category},
        )

    get_venue_repo().invalidate_gu(gu_name)
    emit_observation(
        dataset='mlo.service',
        action='venue-owner-register',
        outcome='success',
        pipeline_name='venue-owner-bridge',
        stage='register',
        state='COMMITTED',
        request_id=request_id,
    )

    return JsonResponse({
        'success': True,
        'message': f"'{venue_name}' 매장이 신규 등록되어 탐색 목록에 반영되었습니다.",
        'venue_id': new_venue_id,
    })
