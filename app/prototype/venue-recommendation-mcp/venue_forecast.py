"""프로젝트 시간대별 예보 계약을 방문 시각에 맞춰 읽습니다."""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from urllib.parse import urlsplit

import httpx


KST = timezone(timedelta(hours=9))
HOUR = timedelta(hours=1)
PROJECT_FORECAST_MAX_AGE = timedelta(minutes=120)
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("시각은 UTC 오프셋을 포함한 ISO 8601 문자열이어야 합니다.")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("방문 시각에 UTC 오프셋을 포함해 주세요. 예를 들어 15:30+09:00입니다.")
    return result.astimezone(KST)


@dataclass(frozen=True)
class Forecast:
    state: str
    provider: str
    reason: str | None = None
    condition: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    issued_at: str | None = None
    precipitation_mm: float | None = None
    queried_region_id: str | None = None

    @property
    def seating_weather(self):
        return "CLEAR" if self.state == "DRY" else self.state


def missing(provider, reason):
    return Forecast("NO_FORECAST", provider, reason)


def project_forecast(payload, location, visit_at, now):
    provider = "project_weather"
    if payload.get("is_test") is True:
        return missing(provider, "TEST_FORECAST")
    if (payload.get("region_id"), payload.get("nx"), payload.get("ny")) != (
        location["region_id"], location["nx"], location["ny"]
    ):
        return missing(provider, "REGION_MISMATCH")

    segments = payload["segments"]
    if not isinstance(segments, list):
        raise ValueError("예보 구간 형식 오류")
    relevant = []
    for segment in segments:
        start, end = timestamp(segment["start_at"]), timestamp(segment["end_at"])
        if start <= visit_at < end:
            relevant.append((segment, start, end))
    if not relevant:
        return missing(provider, "TIME_NOT_COVERED")
    if len(relevant) != 1:
        return missing(provider, "AMBIGUOUS_SEGMENT")
    segment, start, end = relevant[0]
    if segment.get("status") != "valid" or end - start != HOUR:
        return missing(provider, "SEGMENT_NOT_VALID")

    issued = timestamp(segment["issued_at"])
    received = timestamp(segment["received_at"])
    if not issued <= received <= now < issued + PROJECT_FORECAST_MAX_AGE:
        return missing(provider, "STALE_OR_FUTURE_SEGMENT")

    pty = segment.get("pty")
    if type(pty) is not int:
        return missing(provider, "INVALID_PRECIPITATION")
    sky = segment.get("sky")
    if pty == 0 and type(sky) is int and sky in (1, 3, 4):
        state = "DRY"
        condition = {1: "CLEAR", 3: "CLOUDY", 4: "OVERCAST"}[sky]
    elif pty in (1, 2, 4, 5, 6):
        state, condition = "RAIN", "RAIN_SNOW" if pty in (2, 6) else "RAIN"
    elif pty in (3, 7):
        return Forecast("NO_FORECAST", provider, "SNOW_POLICY_UNDEFINED", "SNOW",
                        start.isoformat(), end.isoformat(), issued.isoformat())
    else:
        return missing(provider, "INVALID_PRECIPITATION")
    return Forecast(state, provider, condition=condition, start_at=start.isoformat(),
                    end_at=end.isoformat(), issued_at=issued.isoformat())


def open_meteo_forecast(payload, visit_at):
    provider = "open_meteo"
    end = visit_at.replace(minute=0, second=0, microsecond=0) + HOUR
    start = end - HOUR
    variables = ("precipitation", "rain", "showers")
    units = payload["hourly_units"]
    if payload.get("utc_offset_seconds") != 32400 or any(units.get(key) != "mm" for key in variables):
        return missing(provider, "INVALID_TIMEZONE_OR_UNIT")

    hourly = payload["hourly"]
    times = hourly["time"]
    target = end.strftime("%Y-%m-%dT%H:%M")
    if not isinstance(times, list) or times.count(target) > 1:
        return missing(provider, "INVALID_FORECAST_RESPONSE")
    if target not in times:
        return missing(provider, "TIME_NOT_COVERED")
    index = times.index(target)
    values = [hourly[key][index] for key in variables]
    if any(type(value) not in (int, float) or not isfinite(value) or value < 0 for value in values):
        return missing(provider, "INVALID_PRECIPITATION")
    precipitation, rain, showers = values
    if rain + showers > 0:
        state, condition, reason = "RAIN", "RAIN", None
    elif precipitation == 0:
        state, condition, reason = "DRY", "DRY", None
    else:
        state, condition, reason = "NO_FORECAST", "OTHER_PRECIPITATION", "PRECIPITATION_POLICY_UNDEFINED"
    return Forecast(state, provider, reason, condition, start.isoformat(), end.isoformat(),
                    precipitation_mm=precipitation)


def project_endpoint(base_url):
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise ValueError("기존 날씨 API의 HTTP(S) 기본 주소를 확인해 주세요.")
    return base_url.rstrip("/") + "/api/weather"


async def fetch_forecasts(locations, visit_at, *, project_url=None, demo_open_meteo=False):
    """동일 KMA 격자를 한 번만 조회하고, 결과를 매장별로 연결합니다."""
    provider = "open_meteo" if demo_open_meteo else "project_weather"
    if not locations:
        return {}
    current_hour = datetime.now(KST).replace(minute=0, second=0, microsecond=0)
    if visit_at < current_hour:
        return {venue_id: missing(provider, "PAST_VISIT") for venue_id in locations}
    if not demo_open_meteo and not project_url:
        return {venue_id: missing(provider, "PROVIDER_NOT_CONFIGURED") for venue_id in locations}

    # Open-Meteo is an explicitly selected non-commercial verification source.
    place_key = ("latitude", "longitude") if demo_open_meteo else ("nx", "ny")
    keys = {}
    unique = {}
    results = {}
    for venue_id, location in locations.items():
        if not location or any(location.get(key) is None for key in place_key):
            results[venue_id] = missing(provider, "REGION_UNMAPPED")
            continue
        key = tuple(location[field] for field in place_key)
        unique.setdefault(key, location)
        keys[venue_id] = key

    async with httpx.AsyncClient(timeout=4.0, limits=httpx.Limits(max_connections=6)) as client:
        async def fetch(location):
            try:
                if demo_open_meteo:
                    response = await client.get(OPEN_METEO_URL, params={
                        "latitude": location["latitude"], "longitude": location["longitude"],
                        "hourly": "precipitation,rain,showers", "timezone": "Asia/Seoul",
                        "forecast_days": 7,
                    })
                else:
                    response = await client.get(project_url, params={"region_id": location["region_id"]},
                                                headers={"Cache-Control": "no-cache"})
                response.raise_for_status()
            except httpx.HTTPError:
                return missing(provider, "PROVIDER_UNAVAILABLE")

            try:
                payload = response.json()
                if demo_open_meteo:
                    return open_meteo_forecast(payload, visit_at)
                forecast = project_forecast(payload, location, visit_at, datetime.now(KST))
                return replace(forecast, queried_region_id=location["region_id"])
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError):
                return missing(provider, "INVALID_FORECAST_RESPONSE")

        values = await asyncio.gather(*(fetch(location) for location in unique.values()))
    fetched = dict(zip(unique, values, strict=True))
    results.update({venue_id: fetched[key] for venue_id, key in keys.items()})
    return results
