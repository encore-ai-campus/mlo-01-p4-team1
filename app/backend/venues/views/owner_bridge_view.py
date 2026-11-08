import json
import uuid
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from backend.venues.models.bridge import VenueOwnerBridge
from backend.venues.repositories.csv_lazy_repo import get_venue_repo


@csrf_exempt
@require_POST
def owner_claim_venue_api(request):
    """
    POST /api/owner/venues/claim
    - 사장님이 기존 CSV 매장에 대해 소유권 인증/연결
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)

    owner_id = body.get('owner_id')
    venue_id = body.get('venue_id')
    biz_num = body.get('business_number', '')

    if not owner_id or not venue_id:
        return JsonResponse({'success': False, 'message': 'owner_id와 venue_id는 필수입니다.'}, status=400)

    repo = get_venue_repo()
    venue = repo.get_venue_by_id(venue_id)
    if not venue:
        return JsonResponse({'success': False, 'message': '해당 매장을 찾을 수 없습니다.'}, status=404)

    bridge, created = VenueOwnerBridge.objects.get_or_create(
        venue_id=venue_id,
        defaults={
            'owner_id': owner_id,
            'venue_name': venue['venue_name'],
            'gu_name': venue['gu_name'],
            'road_address': venue['road_address'],
            'category': venue['category'],
            'business_number': biz_num,
            'is_verified': True,
        }
    )

    if not created:
        bridge.owner_id = owner_id
        bridge.business_number = biz_num
        bridge.is_verified = True
        bridge.save()

    return JsonResponse({
        'success': True,
        'message': f"'{venue['venue_name']}' 매장의 점주 인증이 완료되었습니다.",
        'venue_id': venue_id,
    })


@csrf_exempt
@require_POST
def owner_register_venue_api(request):
    """
    POST /api/owner/venues/register
    - 사장님 신규 매장 직접 등록
    """
    try:
        body = json.loads(request.body.decode('utf-8'))
    except Exception:
        return JsonResponse({'success': False, 'message': '잘못된 JSON 형식입니다.'}, status=400)

    owner_id = body.get('owner_id')
    venue_name = body.get('venue_name', '').strip()
    gu_name = body.get('gu_name', '강남구').strip()
    road_address = body.get('road_address', '').strip()
    category = body.get('category', 'CAFE').upper()
    biz_num = body.get('business_number', '')

    if not owner_id or not venue_name:
        return JsonResponse({'success': False, 'message': 'owner_id와 venue_name은 필수입니다.'}, status=400)

    new_venue_id = f"custom_venue_{uuid.uuid4().hex[:10]}"
    bridge = VenueOwnerBridge.objects.create(
        owner_id=owner_id,
        venue_id=new_venue_id,
        venue_name=venue_name,
        gu_name=gu_name,
        road_address=road_address,
        category=category,
        business_number=biz_num,
        is_verified=True,
    )

    return JsonResponse({
        'success': True,
        'message': f"'{venue_name}' 매장이 신규 등록되었습니다.",
        'venue_id': new_venue_id,
    })
