import json
import os
import time

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from backend.venues.services.ranking_service import filter_and_rank_venues
from backend.venues.services.gemini_concierge_service import generate_concierge_briefing
from backend.venues.services.mcp_stdio import VenueMcpRequestError, VenueMcpUnavailable
from backend.venues.services.venue_llm_concierge import (
    CodexCliVenueProvider,
    GeminiVenueProvider,
    LLM_ERROR_HTTP_STATUS,
    VenueLlmError,
    run_venue_llm_concierge,
)
from backend.venues.streaming.observability import emit_observation
from .access import consumer_post, ensure_request_id, parse_json_body


def _llm_availability(mcp_client):
    unavailable = {"configured": False, "provider": None, "mode": None}
    if mcp_client is None:
        return unavailable
    provider_name = os.environ.get("VENUE_LLM_PROVIDER", "gemini").strip()
    if provider_name == "codex_cli":
        provider_type, arguments = CodexCliVenueProvider, (mcp_client,)
    elif provider_name == "gemini":
        provider_type, arguments = GeminiVenueProvider, ()
    else:
        return unavailable

    availability = {"configured": False, "provider": provider_type.name, "mode": provider_type.mode}
    try:
        provider_type(*arguments)
    except VenueLlmError:
        return availability
    return {**availability, "configured": True}


@require_GET
@ensure_csrf_cookie
def ai_concierge_options_api(request):
    client = getattr(request, "scope", {}).get("venue_mcp_client")
    try:
        options = client.list_tags() if client is not None else None
    except (VenueMcpUnavailable, VenueMcpRequestError):
        return JsonResponse({"success": False, "code": "VENUE_MCP_UNAVAILABLE"}, status=503)
    return JsonResponse({"success": True, "mcp": options, "llm": _llm_availability(client)})


@require_POST
def ai_concierge_chat_api(request):
    if request.content_type != "application/json":
        return JsonResponse({"success": False, "code": "JSON_REQUIRED", "message": "JSON 요청이 필요합니다."}, status=415)
    if request.headers.get("Origin") != f"{request.scheme}://{request.get_host()}":
        return JsonResponse({"success": False, "code": "ORIGIN_NOT_ALLOWED", "message": "이 화면에서 질문을 보내 주세요."}, status=403)

    try:
        data = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict) or data.keys() - {"message", "region"}:
        return JsonResponse({"success": False, "code": "INVALID_LLM_REQUEST", "message": "질문 형식을 확인해 주세요."}, status=400)
    message, region = data.get("message"), data.get("region")
    if not isinstance(message, str) or not message.strip() or (region is not None and not isinstance(region, str)):
        return JsonResponse({"success": False, "code": "INVALID_LLM_REQUEST", "message": "질문과 지역을 확인해 주세요."}, status=400)

    result = run_venue_llm_concierge(
        message,
        mcp_client=getattr(request, "scope", {}).get("venue_mcp_client"),
        region=region,
    )
    if result["success"]:
        return JsonResponse(result)

    code = result["code"]
    message = "AI 연결 설정 필요" if code == "LLM_NOT_CONFIGURED" else result["llm"]["message"]
    return JsonResponse({**result, "message": message}, status=LLM_ERROR_HTTP_STATUS.get(code, 502))


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


@require_POST
def visit_recommendation_api(request):
    """Run structured MCP searches with normal session and CSRF checks."""
    if request.content_type != 'application/json':
        return JsonResponse({'success': False, 'code': 'JSON_REQUIRED'}, status=415)
    try:
        body = json.loads(request.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    if not isinstance(body, dict) or set(body) != {'mcp'}:
        return JsonResponse({'success': False, 'code': 'INVALID_MCP_REQUEST'}, status=400)
    return _mcp_recommendation(request, body['mcp'])


@consumer_post
def ai_concierge_api(request, *, consumer):
    """
    POST /api/venues/ai-concierge
    - Gemini 3.8 Flash 기반 AI 컨시어지 추천 브리핑 API (consumer 세션/CSRF 적용 + 관측 로깅)
    """
    t0 = time.monotonic()
    body, _ = parse_json_body(request, required=False)
    body = body or {}

    if isinstance(body, dict) and "mcp" in body:
        return _mcp_recommendation(request, body["mcp"])

    query = str(body.get('query', '') or '').strip()
    gu = str(body.get('gu', '강남구') or '강남구').strip()
    weather = str(body.get('weather', 'CLEAR') or 'CLEAR').strip().upper()
    time_slot = str(body.get('time_slot', 'AFTERNOON') or 'AFTERNOON').strip().upper()
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

    duration_ms = int((time.monotonic() - t0) * 1000)
    emit_observation(
        dataset='mlo.service',
        action='venue-ai-concierge',
        outcome='success',
        pipeline_name='venue-concierge',
        stage='recommend',
        state='SUCCEEDED',
        duration_ms=duration_ms,
        records_out=len(top_venues),
        request_id=ensure_request_id(request),
    )

    return JsonResponse({
        'success': True,
        'briefing': briefing,
        'candidate_venues': top_venues,
    })
