"""Build the hourly playlist from a bounded, indexed candidate pool."""
import os
import time
from collections import Counter
from concurrent.futures import Future
from datetime import timedelta
from threading import Thread

from django.conf import settings
from django.utils import timezone

from . import dispatch
from .errors import DomainError, require
from .gpt_order import GptOrderError, playlist_order_model, propose_order
from .hourly_catalog import candidate_rows
from .hourly_plan import plan_blocks
from .models import Playlist
from .policy import profile
from .weather import current_profile, instant, obtain, validate


HOUR = timedelta(hours=1)
FORECAST_THEMES = {
    'CLEAR': ('BRIGHT', 'ENERGETIC'),
    'CLOUDY': ('CALM', 'PEACEFUL'),
    'OVERCAST': ('DREAMY', 'EMOTIONAL'),
    'RAIN': ('CALM', 'EMOTIONAL'),
    'SNOW': ('PEACEFUL', 'DREAMY'),
    'MIXED': ('CALM', 'DREAMY'),
}


def deadline_for(job):
    return job.created_at + timedelta(seconds=settings.HOURLY_RECOMMENDATION_BUDGET_SECONDS)


def remaining(deadline):
    return (deadline - timezone.now()).total_seconds()


def require_time(deadline):
    require(remaining(deadline) > 0, 'JOB_EXPIRED', '시간별 선곡 작업의 기한이 지났어요.', 410)


def slot_weather(segments, start):
    """Use the dominant forecast in each slot, with the earlier one winning ties."""
    end = start + HOUR
    weights = Counter()
    for segment in segments:
        left = max(start, instant(segment['start_at']))
        right = min(end, instant(segment['end_at']))
        if left < right:
            weights[profile(segment['pty'], segment.get('sky'))] += (right - left).total_seconds()
    require(abs(sum(weights.values()) - 3600) < 0.001,
            'FORECAST_UNAVAILABLE', '선곡 시간대의 예보가 부족해요.', 503)
    return max(weights, key=weights.get)


def retained_plan(parent):
    if parent is None:
        return [], None
    saved = parent.snapshot['hourly_plan']['blocks']
    items = {row['track_id']: row for row in parent.snapshot['items']}
    return [{**block, 'tracks': [items[key] for key in block['track_ids']]} for block in saved[1:]], instant(saved[-1]['end_at'])


def reorder(blocks, orders):
    require(len(orders) == len(blocks), 'GPT_ORDER_INVALID')
    result = []
    for block, keys in zip(blocks, orders, strict=True):
        tracks = {track['track_id']: track for track in block['tracks']}
        require(len(keys) == len(tracks) and set(keys) == tracks.keys(), 'GPT_ORDER_INVALID')
        result.append({**block, 'tracks': [tracks[key] for key in keys]})
    return result


def ask_for_order(blocks, anchor, timeout):
    result = Future()

    def request():
        try:
            result.set_result(propose_order(blocks, timeout_seconds=timeout, anchor=anchor))
        except Exception as error:
            result.set_exception(error)

    Thread(target=request, daemon=True, name='hourly-gpt').start()
    return result


def load_pools(catalog, forecasts, options, excluded, deadline, *, relax_theme=False, rotation_seed=''):
    preferences = set(options['selected_theme_codes'])
    pools = {}
    for weather in dict.fromkeys(forecasts):
        require_time(deadline)
        weather_themes = FORECAST_THEMES.get(weather, ())
        rows = candidate_rows(
            catalog, genres=options['selected_genre_codes'], themes=preferences,
            weather_themes=weather_themes, excluded=excluded,
            instrumental_only=options['vocal_option'] == 'instrumental_only',
            limit=settings.HOURLY_RECOMMENDATION_CANDIDATE_LIMIT, allow_theme_fallback=relax_theme,
            rotation_seed=rotation_seed,
        )
        ranked = []
        for row in rows:
            themes = set(row.get('theme_codes') or ())
            matches_preference = bool(themes & preferences)
            matches_weather = bool(themes.intersection(weather_themes))
            score = 80 * matches_preference + 20 * matches_weather
            ranked.append({**row, 'scores': {'W': '100' if matches_weather else '0' if weather else None,
                                            'P': '100' if matches_preference else '0' if preferences else None,
                                            'S': str(score)}})
        pools[weather] = ranked
    return [pools[weather] for weather in forecasts]


def prepare(job, context, catalog):
    deadline = deadline_for(job)
    require_time(deadline)
    options = context['options']
    parent = Playlist.objects.get(pk=job.parent_id, family_id=job.family_id) if job.parent_id else None
    regenerating = bool(context.get('hourly_regeneration'))
    retained, append_at = retained_plan(None if regenerating else parent)
    previous_track_ids = {row['track_id'] for row in parent.snapshot['items']} if regenerating and parent else ()
    kept = [row for block in retained for row in block['tracks']]
    excluded = set(context['excluded_track_ids'])
    require(not excluded.intersection(row['track_id'] for row in kept), 'HOURLY_EXCLUSIONS_CHANGED',
            '유지 구간에 새로 제외한 곡이 있어요. 새 선곡표를 만들어 주세요.', 409)
    now = timezone.now()
    on = options['weather_influence_mode'] == 'ON'
    bundle = obtain(options['region_id'], now) if on else None
    start = append_at or (instant(bundle['as_of']) if bundle else now)
    count = 1 if retained else 3
    if bundle:
        horizon = start + count * HOUR - instant(bundle['as_of'])
        segments = validate(bundle, now, duration=round(horizon.total_seconds() * 1000))
        forecasts = [slot_weather(segments, start + index * HOUR) for index in range(count)]
        context.update(weather=bundle, weather_code=current_profile(bundle, segments, now))
    else:
        forecasts = [None] * count

    excluded.update(row['track_id'] for row in kept)
    anchor = kept[-1] if kept else None
    began_candidates = time.monotonic()
    pools = load_pools(catalog, forecasts, options, excluded, deadline, rotation_seed=job.pk)
    candidate_ms = round((time.monotonic() - began_candidates) * 1000)
    selection = dict(start_at=start, forecasts=forecasts, retained=retained, anchor=anchor,
                     seed=job.pk if regenerating else job.family_id, previous_track_ids=previous_track_ids)
    theme_relaxed = False
    has_theme = bool(options['selected_theme_codes'])
    began_rule = time.monotonic()
    try:
        seconds = max(0, remaining(deadline) - 0.25)
        blocks = plan_blocks(pools, **selection, deadline=time.monotonic() + (min(0.9, seconds) if has_theme else seconds))
    except DomainError as error:
        if not has_theme or error.code not in {'CANDIDATE_SHORTAGE', 'SEARCH_INCOMPLETE', 'NO_MEANINGFUL_CHANGE'}:
            raise
        pools = load_pools(catalog, forecasts, options, excluded, deadline, relax_theme=True, rotation_seed=job.pk)
        blocks = plan_blocks(pools, **selection, deadline=time.monotonic() + max(0, remaining(deadline) - 0.25))
        theme_relaxed = True
    diagnostics = {'status': 'unconfigured', 'candidate_counts': [len(pool) for pool in pools],
                   'candidate_ms': candidate_ms, 'rule_with_relaxation_ms': round((time.monotonic() - began_rule) * 1000),
                   'theme_relaxed': theme_relaxed}
    model = playlist_order_model()
    if os.environ.get('OPENAI_API_KEY', '').strip():
        timeout = min(settings.HOURLY_GPT_TIMEOUT_SECONDS, remaining(deadline) - 0.5)
        diagnostics['status'] = 'budget_or_capacity'
        if timeout > 0.15 and dispatch.try_begin_hourly_provider(job, timeout):
            began = time.monotonic()
            response = ask_for_order(blocks, anchor, timeout)
            try:
                ordered = reorder(blocks, response.result(timeout=timeout))
                if remaining(deadline) > 0.25:
                    blocks = ordered
                    diagnostics.update(status='accepted', model=model)
            except (GptOrderError, TimeoutError, TypeError, ValueError):
                diagnostics['status'] = 'rule_fallback'
            except DomainError as error:
                if error.code != 'GPT_ORDER_INVALID':
                    raise
                diagnostics['status'] = 'rule_fallback'
            finally:
                if response.done():
                    dispatch.end_provider(job)
                diagnostics['provider_ms'] = round((time.monotonic() - began) * 1000)
    require_time(deadline)
    if bundle:
        require(instant(bundle['valid_until']) > timezone.now(), 'JOB_EXPIRED', '선곡 예보가 만료됐어요.', 410)
    all_blocks = [*retained, *blocks]
    public = [{**{key: value for key, value in block.items() if key != 'tracks'},
               'track_ids': [row['track_id'] for row in block['tracks']]} for block in all_blocks]
    items = [{**{key: value for key, value in track.items() if key not in {'score_cache', 'speed'}},
              'scores': track.get('scores', {'W': None, 'P': None, 'S': None})}
             for block in all_blocks for track in block['tracks']]
    total = sum(block['duration_ms'] for block in all_blocks)
    return items, total, {'blocks': public, 'next_refresh_at': public[0]['end_at']}, diagnostics
