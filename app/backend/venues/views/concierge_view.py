import json
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import csrf_exempt

from backend.venues.services.ranking_service import filter_and_rank_venues
from backend.venues.services.gemini_concierge_service import generate_concierge_briefing


@csrf_exempt
@require_POST
def ai_concierge_api(request):
    """
    POST /api/venues/ai-concierge
    - Gemini 3.8 Flash (gemini-3.8-flash) 기반 AI 컨시어지 추천 브리핑 API
    """
    try:
        body = json.loads(request.body.decode('utf-8')) if request.body else {}
    except Exception:
        body = {}

    query = body.get('query', '').strip()
    gu = body.get('gu', '강남구').strip()
    weather = body.get('weather', 'CLEAR').strip().upper()
    time_slot = body.get('time_slot', 'AFTERNOON').strip().upper()
    budget_tier = body.get('budget_tier')
    category = body.get('category', 'ALL')

    # 후보 매장 상위 5곳 추출
    top_venues = filter_and_rank_venues(
        gu=gu,
        category=category,
        budget_tier=budget_tier,
        weather=weather,
        time_slot=time_slot,
        limit=5,
    )

    briefing = generate_concierge_briefing(
        user_query=query,
        top_venues=top_venues,
        current_weather=weather,
        gu_name=gu,
        budget_tier=budget_tier,
    )

    return JsonResponse({
        'success': True,
        'briefing': briefing,
        'candidate_venues': top_venues,
    })
