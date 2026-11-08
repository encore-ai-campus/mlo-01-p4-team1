from typing import Any, Dict, Optional
from backend.venues.models.bridge import VenueOwnerBridge


def get_venue_music_banner(
    venue: Dict[str, Any],
    current_weather: str = 'CLEAR',
) -> Optional[Dict[str, Any]]:
    """
    파트너 매장(is_partner=True)인 경우에만 음악 배너 데이터를 바인딩.
    일반 매장인 경우 None 반환.
    - 1순위: Track 1 점주 승인 선곡표 (VenueOwnerBridge.track1_partner_music_id 연동)
    - 2순위: 동네 날씨 맞춤 선곡 테마 배너
    """
    is_partner = venue.get('is_partner', False)
    if isinstance(is_partner, str):
        is_partner = is_partner.lower() in ('true', 'y', '1')

    if not is_partner:
        return None

    venue_id = venue.get('venue_id', '')
    gu_name = venue.get('gu_name', '서울')
    venue_name = venue.get('venue_name', '')
    music_mood = venue.get('music_mood', 'LOFI')

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
            'badge_text': '🎵 파트너 매장 선곡표 재생 중',
        }

    # 2순위: 동네 날씨 맞춤 선곡 테마 배너
    weather_upper = current_weather.upper()
    if 'RAIN' in weather_upper or '비' in weather_upper:
        theme_title = f"{gu_name} 비 오는 날의 감성 재즈 & 로파이"
        note = "창밖의 빗소리와 가장 잘 어울리는 어쿠스틱 선곡표를 감상해보세요."
    elif 'SNOW' in weather_upper or '눈' in weather_upper:
        theme_title = f"{gu_name} 하얀 눈 내리는 날의 윈터 멜로디"
        note = "눈 내리는 날 따뜻한 실내 분위기를 돋우는 스페셜 선곡표입니다."
    else:
        theme_title = f"{gu_name} 오후 햇살을 닮은 시티 팝 & 칠합"
        note = "기분 좋은 맑은 날씨에 어울리는 경쾌하고 청량한 선곡표입니다."

    return {
        'has_music_banner': True,
        'banner_type': 'WEATHER_THEME',
        'playlist_id': f"theme_{gu_name}_{weather_upper.lower()}",
        'playlist_title': theme_title,
        'curator_note': note,
        'music_mood': music_mood,
        'badge_text': '🎶 날씨 맞춤 공간 BGM',
    }
