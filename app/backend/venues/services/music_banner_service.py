from typing import Any, Dict, Optional
from backend.venues.models.bridge import VenueOwnerBridge


def get_venue_music_banner(
    venue: Dict[str, Any],
    current_weather: str = 'CLEAR',
) -> Optional[Dict[str, Any]]:
    """
    방문객 대상 매장 분위기 및 오늘 날씨 맞춤 추천 음악(BGM 플레이리스트 큐레이션) 배너 데이터를 바인딩.
    - 1순위: 점주가 등록/승인한 공식 선곡표가 있는 경우 (VenueOwnerBridge.track1_partner_music_id 연동)
    - 2순위: 매장별 '추천음악_스타일'(recommendation_style) 및 동네 실시간 날씨 맞춤 BGM 큐레이션
    """
    venue_id = venue.get('venue_id', '')
    gu_name = venue.get('gu_name', '서울')
    venue_name = venue.get('venue_name', '')
    music_mood = venue.get('music_mood', 'LOFI')
    rec_style = venue.get('recommendation_style') or venue.get('추천음악_스타일') or ''

    # 1순위: 점주 등록 브릿지 테이블 조회
    bridge = VenueOwnerBridge.objects.filter(venue_id=venue_id).first()
    if bridge and bridge.track1_partner_music_id:
        return {
            'has_music_banner': True,
            'banner_type': 'APPROVED_PLAYLIST',
            'playlist_id': bridge.track1_partner_music_id,
            'playlist_title': f"{venue_name} 공식 추천 플레이리스트",
            'curator_note': f"매장 점주가 오늘 날씨({current_weather})에 맞춰 직접 승인한 공식 선곡표입니다.",
            'music_mood': music_mood,
            'badge_text': '🎵 매장 공식 선곡표',
        }

    # 2순위: 매장 분위기·업종 기반 추천음악 스타일 & 동네 날씨 맞춤 선곡 큐레이션 배너
    weather_upper = current_weather.upper()
    if 'RAIN' in weather_upper or '비' in weather_upper:
        theme_title = rec_style or f"{gu_name} 비 오는 날의 감성 재즈 & 로파이"
        note = "창밖의 빗소리와 매장 분위기에 가장 잘 어울리는 추천 BGM 플레이리스트입니다."
    elif 'SNOW' in weather_upper or '눈' in weather_upper:
        theme_title = rec_style or f"{gu_name} 하얀 눈 내리는 날의 윈터 멜로디"
        note = "눈 내리는 날 따뜻한 실내 분위기를 돋우는 공간 맞춤 추천 선곡표입니다."
    else:
        theme_title = rec_style or f"{gu_name} 오후 햇살을 닮은 어쿠스틱 & 시티팝"
        note = "오늘 날씨와 이 매장의 무드에 어울리는 방문객 맞춤 BGM 큐레이션입니다."

    return {
        'has_music_banner': True,
        'banner_type': 'WEATHER_THEME',
        'playlist_id': f"theme_{gu_name}_{weather_upper.lower()}",
        'playlist_title': theme_title,
        'curator_note': note,
        'music_mood': music_mood,
        'badge_text': '🎧 공간 & 날씨 맞춤 추천 BGM',
    }
