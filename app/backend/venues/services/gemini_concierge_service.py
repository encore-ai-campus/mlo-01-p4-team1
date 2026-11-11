import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GEMINI_MODEL_NAME = 'gemini-3.8-flash'


def generate_concierge_briefing(
    user_query: str,
    top_venues: List[Dict[str, Any]],
    current_weather: str = 'CLEAR',
    gu_name: str = '강남구',
    budget_tier: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Gemini 3.8 Flash (gemini-3.8-flash) 기반 AI 매장·음식·분위기 컨시어지 서비스.
    - 유저의 자연어 질문과 필터링된 상위 매장 목록을 종합 분석하여 브리핑 제공.
    - GEMINI_API_KEY 미설정 또는 호출 실패 시 스마트 규칙 기반 폴백으로 100% 정상 응답.
    """
    if not top_venues:
        return {
            'concierge_title': f'{gu_name} 맞춤 매장 탐색 결과',
            'summary_briefing': f'현재 {gu_name} 조건에 부합하는 매장을 찾지 못했습니다. 필터 조건을 완화해보세요.',
            'recommended_venue_id': None,
            'recommended_venue_name': None,
            'recommended_menu_combination': [],
            'reasoning': '해당 조건의 후보 매장이 없습니다.',
            'model_used': 'smart-rule-fallback',
        }

    api_key = os.environ.get('GEMINI_API_KEY', '').strip()

    # 1. Gemini API 키가 존재하는 경우 실제 호출 시도
    if api_key:
        try:
            return _call_gemini_api(
                api_key=api_key,
                user_query=user_query,
                top_venues=top_venues[:5],
                current_weather=current_weather,
                gu_name=gu_name,
                budget_tier=budget_tier,
            )
        except Exception as e:
            logger.warning(f"[GeminiConcierge] API call failed ({e}), using smart fallback.")

    # 2. API 키 미설정 또는 실패 시 스마트 도메인 규칙 기반 브리핑 폴백
    return _generate_smart_rule_briefing(
        user_query=user_query,
        top_venues=top_venues,
        current_weather=current_weather,
        gu_name=gu_name,
        budget_tier=budget_tier,
    )


def _call_gemini_api(
    api_key: str,
    user_query: str,
    top_venues: List[Dict[str, Any]],
    current_weather: str,
    gu_name: str,
    budget_tier: Optional[int],
) -> Dict[str, Any]:
    """Google GenAI SDK 또는 REST API를 통한 Gemini 3.8 Flash 호출"""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    # 매장 정보 요약 컨텍스트 생성
    venue_context = []
    for v in top_venues:
        menus_summary = ', '.join([f"{m['menu_name']}({m['price']:,}원)" for m in v.get('matched_menus_preview', [])[:4]])
        venue_context.append(
            f"- 매장 ID: {v['venue_id']}, 매장명: {v['venue_name']}, 업종: {v['category']}, "
            f"분위기: {','.join(v.get('mood_tags', []))}, 대표메뉴: {menus_summary}"
        )

    prompt = f"""당신은 날씨와 예산, 고객의 기분을 완벽하게 맞춤 안내하는 전문 매장 & 미식 컨시어지 AI입니다.
[현재 상황]
- 자치구: {gu_name}
- 현재 날씨: {current_weather}
- 예산 단계: {budget_tier or '선택없음'}
- 고객의 질문 및 고민: "{user_query}"

[선정된 상위 후보 매장 목록]
{chr(10).join(venue_context)}

위 후보 매장 중 고객의 질문과 오늘 날씨에 가장 잘 어울리는 1곳을 엄선하고, 반드시 다음 JSON 형식으로만 응답하세요:
{{
  "concierge_title": "고객을 위한 매력적인 한줄 타이틀",
  "summary_briefing": "오늘 날씨와 분위기를 엮은 다정한 브리핑 코멘트 (2~3문장)",
  "recommended_venue_id": "선정한 매장의 venue_id",
  "recommended_venue_name": "선정한 매장의 매장명",
  "recommended_menu_combination": ["어울리는 메뉴1", "어울리는 메뉴2"],
  "reasoning": "이 매장과 메뉴 조합을 추천한 명확한 이유 (1~2문장)"
}}
"""

    response = client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        )
    )

    result_json = json.loads(response.text)
    result_json['model_used'] = GEMINI_MODEL_NAME
    return result_json


def _generate_smart_rule_briefing(
    user_query: str,
    top_venues: List[Dict[str, Any]],
    current_weather: str,
    gu_name: str,
    budget_tier: Optional[int],
) -> Dict[str, Any]:
    """도메인 규칙 기반 고도화 스마트 브리핑 (오프라인/무료 폴백)"""
    top_v = top_venues[0]
    v_name = top_v.get('venue_name', '')
    v_id = top_v.get('venue_id', '')
    cat = top_v.get('category', 'CAFE')
    moods = top_v.get('mood_tags', [])
    menus = top_v.get('matched_menus_preview', [])

    w_upper = current_weather.upper()
    is_rain = 'RAIN' in w_upper or '비' in w_upper or '전' in user_query or '막걸리' in user_query
    is_night = cat == 'PUB' or '술' in user_query or '하이볼' in user_query or '맥주' in user_query

    # 추천 메뉴 조합 선정
    combo = []
    for m in menus[:2]:
        combo.append(m['menu_name'])
    if not combo:
        combo = ['시그니처 메뉴 세트']

    if is_rain:
        title = f"비 내리는 {gu_name}, 따뜻한 감성을 채워줄 맞춤 공간"
        briefing = f"오늘처럼 비 오는 날에는 창밖을 바라보며 아늑한 분위기를 즐길 수 있는 '{v_name}'을(를) 추천드려요. 빗소리와 함께 편안한 대화를 나누기에 가장 좋은 공간입니다."
        reasoning = f"매장의 대표 감성 키워드({', '.join(moods[:2])})와 날씨 적합도가 뛰어나며, {combo[0]} 메뉴가 비 오는 날 분위기와 완벽한 조화를 이룹니다."
    elif is_night:
        title = f"하루의 피로를 풀어줄 {gu_name}의 감성 아지트"
        briefing = f"오늘 저녁 소중한 사람들과 가볍게 한잔 기울이기 좋은 '{v_name}'을(를) 추천합니다. 감각적인 인테리어와 음악이 어우러져 기분 좋은 밤을 만들어드립니다."
        reasoning = f"추천 메뉴인 {', '.join(combo)}의 페어링이 훌륭하며, 현재 영업 중으로 방문하시기에 가장 적합합니다."
    else:
        title = f"화창한 {gu_name}의 여유를 만끽할 수 있는 스페셜 플레이스"
        briefing = f"오늘 날씨와 잘 어울리는 밝고 경쾌한 무드의 '{v_name}'에서 기분 좋은 휴식을 즐겨보세요. 여유로운 시간과 함께 정성껏 준비된 메뉴를 맛보실 수 있습니다."
        reasoning = f"고객님의 취향에 부합하는 {combo[0]} 메뉴와 매장의 독보적인 시그니처 분위기가 높은 만족도를 제공합니다."

    return {
        'concierge_title': title,
        'summary_briefing': briefing,
        'recommended_venue_id': v_id,
        'recommended_venue_name': v_name,
        'recommended_menu_combination': combo,
        'reasoning': reasoning,
        'model_used': 'smart-rule-fallback',
    }
