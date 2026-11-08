import datetime
import re
from typing import Optional, Tuple


def parse_time_str(time_str: str) -> Optional[datetime.time]:
    """'09:00', '17:30', '3:00' 형식의 문자열을 datetime.time 객체로 파싱"""
    if not time_str:
        return None
    time_str = time_str.strip()
    match = re.search(r'(\d{1,2}):(\d{2})', time_str)
    if match:
        h, m = int(match.group(1)), int(match.group(2))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return datetime.time(h, m)
    return None


def parse_hours_range(hours_str: str) -> Tuple[Optional[datetime.time], Optional[datetime.time]]:
    """'17:00 ~ 03:00', '09:00-22:00' 등의 영업시간 문자열 분리"""
    if not hours_str:
        return None, None
    parts = re.split(r'~|-', hours_str)
    if len(parts) >= 2:
        return parse_time_str(parts[0]), parse_time_str(parts[1])
    return None, None


def check_business_hours(
    open_time: Optional[datetime.time],
    close_time: Optional[datetime.time],
    current_time: Optional[datetime.time] = None,
) -> bool:
    """
    영업 시간 판정 핵심 로직 (심야 자정 넘김 완벽 지원).
    - 일반 영업: open <= now < close (예: 09:00 ~ 22:00)
    - 심야 영업: now >= open OR now < close (예: 17:00 ~ 03:00)
    """
    if open_time is None or close_time is None:
        # 정보 누락 시 기본적으로 영업 중으로 판정
        return True

    if current_time is None:
        current_time = datetime.datetime.now().time()

    if open_time == close_time:
        # 24시간 영업
        return True

    if open_time < close_time:
        # 일반 당일 영업 (예: 09:00 ~ 22:00)
        return open_time <= current_time < close_time
    else:
        # 심야 영업 (자정 넘김, 예: 17:00 ~ 익일 03:00)
        return current_time >= open_time or current_time < close_time


def is_venue_open_now(
    venue: dict,
    current_time: Optional[datetime.time] = None,
) -> bool:
    """매장 딕셔너리 정보 기반 현재 영업 중 여부 판정"""
    # 1. 명시적인 open_time, close_time 필드 확인
    open_time = parse_time_str(venue.get('open_time', ''))
    close_time = parse_time_str(venue.get('close_time', ''))

    # 2. open_hours 텍스트 필드 확인
    if open_time is None or close_time is None:
        hours_str = venue.get('open_hours', '')
        parsed_open, parsed_close = parse_hours_range(hours_str)
        if parsed_open and parsed_close:
            open_time, close_time = parsed_open, parsed_close

    # 3. 데이터에 영업시간 필드가 없는 경우 카테고리/시간대별 기본값 부여
    if open_time is None or close_time is None:
        cat = venue.get('category', 'CAFE').upper()
        time_slot = venue.get('recommended_time_slot', 'AFTERNOON').upper()
        if cat == 'PUB' or time_slot == 'NIGHT':
            # 주점/심야 기본값: 17:00 ~ 익일 03:00
            open_time = datetime.time(17, 0)
            close_time = datetime.time(3, 0)
        else:
            # 카페/주간 기본값: 08:30 ~ 22:00
            open_time = datetime.time(8, 30)
            close_time = datetime.time(22, 0)

    return check_business_hours(open_time, close_time, current_time)
