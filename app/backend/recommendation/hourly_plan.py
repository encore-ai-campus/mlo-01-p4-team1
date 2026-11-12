"""Combine prepared weather-slot candidates into one rolling playlist window."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from time import monotonic

from ortools.sat.python import cp_model

from .errors import DomainError, require
from .youtube_registry import normalize_video_url


MINUTE_MS = 60_000
SLOT_TARGET_MS = 60 * MINUTE_MS
SLOT_MIN_MS = 55 * MINUTE_MS
SLOT_MAX_MS = 65 * MINUTE_MS
WINDOW_MIN_MS = 175 * MINUTE_MS
WINDOW_MAX_MS = 185 * MINUTE_MS
DEFAULT_PLAN_SECONDS = 2.0


def _identity_keys(track):
    keys = {('track', track['track_id'])}
    for field in ('youtube_video_id', 'video_id'):
        if isinstance(value := track.get(field), str) and value.strip():
            keys.add(('video', value.strip()))
    if isinstance(url := track.get('play_url'), str) and 'youtu' in url:
        try:
            keys.add(('video', normalize_video_url(url)))
        except DomainError:
            pass  # Other playback providers and legacy URLs can still use the track ID.
    if isinstance(group := track.get('recording_group_id'), str) and group.strip():
        keys.add(('recording', group.strip()))
    if isinstance(audio_hash := track.get('audio_sha256'), str) and audio_hash.strip():
        keys.add(('audio', audio_hash.strip().lower()))
    if track['track_id'].startswith('tagged:'):
        keys.add(('audio', track['track_id'].split(':', 1)[1].lower()))
    if isinstance(isrc := track.get('isrc'), str) and isrc.strip():
        keys.add(('isrc', isrc.strip().replace('-', '').upper()))
    return frozenset(keys)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _features(track):
    audio = tuple(_number(track.get('raw_' + field)) for field in ('energy', 'acousticness', 'valence'))
    return (audio, frozenset(track.get('theme_codes') or ()), frozenset(track.get('genre_group_codes') or ()),
            str(track.get('artists_id') or track.get('artists_name') or '').strip().casefold())


def _tag_distance(first, second):
    return 1 - len(first & second) / len(first | second) if first and second else 0.5


def _transition(previous, next_):
    audio, themes, genres, artist = previous
    next_audio, next_themes, next_genres, next_artist = next_
    distances = [abs(a - b) for a, b in zip(audio, next_audio) if a is not None and b is not None]
    audio_distance = sum(distances) / len(distances) if distances else 0.5
    return (audio_distance + _tag_distance(themes, next_themes) + _tag_distance(genres, next_genres),
            bool(artist and artist == next_artist))


@dataclass(frozen=True)
class _Candidate:
    track: dict
    keys: frozenset
    quality: float
    tie: str

    @property
    def duration(self):
        return self.track['duration_ms']


def _candidate(track, seed, slot):
    track_id = track['track_id']
    tie = sha256(f'{seed}\0{slot}\0{track_id}'.encode()).hexdigest() if seed else track_id
    quality = _number((track.get('scores') or {}).get('S')) or 0.0
    return _Candidate(track, _identity_keys(track), quality, tie)


def _valid_track(track):
    return (isinstance(track, dict) and isinstance(track.get('track_id'), str) and bool(track['track_id'].strip())
            and type(track.get('duration_ms')) is int and 0 < track['duration_ms'] <= SLOT_MAX_MS)


def _greedy(pools, retained_duration):
    key_slots = defaultdict(set)
    for index, pool in enumerate(pools):
        for candidate in pool:
            for key in candidate.keys:
                key_slots[key].add(index)
    selected = [[] for _ in pools]
    totals = [0 for _ in pools]
    used = set()
    for index in sorted(range(len(pools)), key=lambda index: len(pools[index])):
        ranked = sorted(pools[index], key=lambda candidate: (
            -candidate.quality, max(len(key_slots[key]) for key in candidate.keys), candidate.tie))
        prefix, total = [], 0
        for candidate in ranked:
            proposed = total + candidate.duration
            if candidate.keys & used or proposed > SLOT_MAX_MS:
                continue
            if total >= SLOT_MIN_MS and abs(proposed - SLOT_TARGET_MS) >= abs(total - SLOT_TARGET_MS):
                continue
            prefix.append(candidate)
            total = proposed
            used.update(candidate.keys)
            if total == SLOT_TARGET_MS:
                break
        if total < SLOT_MIN_MS:
            return None
        selected[index], totals[index] = prefix, total
    return selected if WINDOW_MIN_MS <= retained_duration + sum(totals) <= WINDOW_MAX_MS else None


def _deadline(remaining):
    require(remaining > 0, 'SEARCH_INCOMPLETE', '시간 안에 시간대별 선곡을 완료하지 못했어요.', 503)
    return remaining


def _constraint_search(pools, retained_duration, deadline, previous_track_ids, minimum_new):
    """Find a feasible cross-slot assignment when the quick greedy pass misses one."""
    model = cp_model.CpModel()
    picks_by_key = defaultdict(list)
    variables, durations, fresh_picks = [], [], []
    for slot, pool in enumerate(pools):
        picks = [model.new_bool_var(f'slot_{slot}_{i}') for i in range(len(pool))]
        variables.append(picks)
        for candidate, pick in zip(pool, picks):
            if minimum_new and candidate.track['track_id'] not in previous_track_ids:
                fresh_picks.append(pick)
            for key in candidate.keys:
                picks_by_key[key].append(pick)
        duration = sum(candidate.duration * pick for candidate, pick in zip(pool, picks))
        model.add_linear_constraint(duration, SLOT_MIN_MS, SLOT_MAX_MS)
        durations.append(duration)
    for key in sorted(picks_by_key):
        picks = picks_by_key[key]
        if len(picks) > 1:
            model.add_at_most_one(picks)
    model.add_linear_constraint(sum(durations) + retained_duration, WINDOW_MIN_MS, WINDOW_MAX_MS)
    if minimum_new:
        model.add(sum(fresh_picks) >= minimum_new)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    remaining = _deadline(deadline - monotonic())
    solver.parameters.max_time_in_seconds = remaining - min(0.05, remaining / 10)
    status = solver.solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [[candidate for candidate, pick in zip(pool, picks) if solver.value(pick)]
                for pool, picks in zip(pools, variables)]
    require(status != cp_model.INFEASIBLE, 'CANDIDATE_SHORTAGE',
            '제공된 후보로 시간과 중복 조건을 함께 맞출 수 없어요.', scope='supplied_pools')
    raise DomainError('SEARCH_INCOMPLETE', '시간 안에 시간대별 선곡을 완료하지 못했어요.', 503)


def _ordered(candidates, previous):
    remaining = [(candidate, _features(candidate.track)) for candidate in candidates]
    ordered = []
    while remaining:
        if previous is None:
            best = min(remaining, key=lambda pair: (-pair[0].quality, pair[0].tie))
        else:
            best = min(remaining, key=lambda pair: (*_transition(previous, pair[1]), -pair[0].quality, pair[0].tie))
        remaining.remove(best)
        candidate, previous = best
        ordered.append(candidate.track)
    return ordered, previous


def _start(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if isinstance(parsed, datetime) and parsed.utcoffset() is not None:
            return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    raise DomainError('VALIDATION_ERROR', '시간대별 선곡의 시작 시각을 확인해 주세요.')


def plan_blocks(pools, *, start_at, forecasts, retained=(), anchor=None, seed='',
                previous_track_ids=(), deadline=None):
    """Return three new blocks or one append block using already filtered candidates.

    Each block keeps the supplied forecast descriptor and original track dicts. Its
    ISO timestamps describe a fixed forecast hour, and duration_ms is actual playback.
    The optional deadline is an absolute monotonic timestamp.
    """
    deadline = monotonic() + DEFAULT_PLAN_SECONDS if deadline is None else deadline
    _deadline(deadline - monotonic())
    require((len(pools), len(retained)) in ((3, 0), (1, 2)) and len(forecasts) == len(pools),
            message='시간대별 후보와 유지 구간 수를 확인해 주세요.')
    start = _start(start_at)
    retained_keys, retained_duration = set(), 0
    for block in retained:
        duration = 0
        for track in block['tracks']:
            require(_valid_track(track), message='유지할 선곡표의 곡 정보를 확인해 주세요.')
            keys = _identity_keys(track)
            require(not keys & retained_keys, message='유지할 선곡표에 중복 곡이 있어요.')
            retained_keys.update(keys)
            duration += track['duration_ms']
        require(duration == block['duration_ms'] and SLOT_MIN_MS <= duration <= SLOT_MAX_MS,
                message='유지할 선곡표의 재생 시간을 확인해 주세요.')
        retained_duration += duration

    candidates = []
    for slot, pool in enumerate(pools):
        _deadline(deadline - monotonic())
        rows = [_candidate(track, seed, slot) for track in pool if _valid_track(track)]
        usable = sorted((row for row in rows if not row.keys & retained_keys),
                        key=lambda row: (-row.quality, row.tie))
        require(sum(row.duration for row in usable) >= SLOT_MIN_MS, 'CANDIDATE_SHORTAGE',
                '제공된 시간대별 후보의 재생 시간이 부족해요.', scope='supplied_pools', slot=slot)
        candidates.append(usable)

    previous_track_ids = frozenset(previous_track_ids)
    minimum_new = max(1, len(previous_track_ids) // 5) if previous_track_ids else 0
    if minimum_new:
        fresh_ids = {row.track['track_id'] for pool in candidates for row in pool} - previous_track_ids
        require(len(fresh_ids) >= minimum_new, 'NO_MEANINGFUL_CHANGE',
                '필요한 신규 곡 수를 채우지 못했어요. 현재 선곡표는 유지됩니다.',
                parent_track_count=len(previous_track_ids), required_new_count=minimum_new,
                actual_new_count=len(fresh_ids))
    selected = _greedy(candidates, retained_duration)
    if selected is not None and minimum_new:
        new_count = sum(row.track['track_id'] not in previous_track_ids for block in selected for row in block)
        if new_count < minimum_new:
            selected = None
    if selected is None:
        selected = _constraint_search(candidates, retained_duration, deadline, previous_track_ids, minimum_new)
    if anchor is None and retained:
        anchor = retained[-1]['tracks'][-1]
    previous = _features(anchor) if anchor is not None else None
    result = []
    for slot, (rows, forecast) in enumerate(zip(selected, forecasts)):
        _deadline(deadline - monotonic())
        tracks, previous = _ordered(rows, previous)
        result.append({'start_at': (start + timedelta(hours=slot)).isoformat(),
                       'end_at': (start + timedelta(hours=slot + 1)).isoformat(),
                       'weather': forecast, 'tracks': tracks,
                       'duration_ms': sum(row.duration for row in rows)})
    _deadline(deadline - monotonic())
    return result
