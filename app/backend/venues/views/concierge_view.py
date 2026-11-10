import json
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import csrf_exempt

from backend.venues.services.ranking_service import filter_and_rank_venues
from backend.venues.services.gemini_concierge_service import generate_concierge_briefing
from backend.venues.services.mcp_stdio import VenueMcpRequestError, VenueMcpUnavailable


@require_GET
def ai_concierge_options_api(request):
    client = getattr(request, "scope", {}).get("venue_mcp_client")
    if client is None:
        return JsonResponse({"success": True, "mcp": None})

    try:
        return JsonResponse({"success": True, "mcp": client.list_tags()})
    except (VenueMcpUnavailable, VenueMcpRequestError):
        return JsonResponse({"success": False, "code": "VENUE_MCP_UNAVAILABLE"}, status=503)


def _mcp_recommendation(request, arguments):
    if not isinstance(arguments, dict):
        return JsonResponse({"success": False, "code": "INVALID_MCP_REQUEST"}, status=400)

    client = getattr(request, "scope", {}).get("venue_mcp_client")
    if client is None:
        return JsonResponse({"success": False, "code": "VENUE_MCP_UNAVAILABLE"}, status=503)

    try:
        result = client.recommend(arguments)
    except VenueMcpRequestError:
        return JsonResponse({"success": False, "code": "MCP_REQUEST_REJECTED"}, status=422)
    except VenueMcpUnavailable:
        return JsonResponse({"success": False, "code": "VENUE_MCP_UNAVAILABLE"}, status=503)
    return JsonResponse({"success": True, "mcp": result})


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

    if isinstance(body, dict) and "mcp" in body:
        return _mcp_recommendation(request, body["mcp"])

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
