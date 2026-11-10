"""격자별 최신 회차 병합. DB 접속 없이 정제 행 목록만 선택."""
from datetime import datetime, timedelta

from pipeline.weather.common import OBS, FORECAST


class WeatherValueConflict(ValueError):
    """같은 발표·수신 시각에 서로 다른 날씨 값이 도착함."""


def _rule_number(row):
    return int(row['rule_version'].removeprefix('normalize_v'))


def _row_key(row):
    return (row['source'], row['issued_at'], row['target_at'], row['category'])


def _business_value(row):
    measurement = {key: value for key, value in row['measurement'].items() if key != 'raw'}
    return measurement, row.get('spatial_basis'), row.get('source_ix'), row.get('source_iy')


def newer(candidate, previous):
    """같은 항목의 두 행 중 candidate를 채택해야 하면 True."""
    same_original = (candidate['issued_at'], candidate['source_hash']) == (
        previous['issued_at'], previous['source_hash']
    )
    if same_original:
        candidate_rule = _rule_number(candidate)
        previous_rule = _rule_number(previous)
        if candidate_rule != previous_rule:
            return candidate_rule > previous_rule
    candidate_order = (candidate['issued_at'], candidate['received_at'], candidate['source_hash'])
    previous_order = (previous['issued_at'], previous['received_at'], previous['source_hash'])
    return candidate_order > previous_order


def _checked_candidates(rows):
    """같은 원문의 최신 정제 규칙을 적용한 뒤 수신 시각별 값 충돌 검사."""
    latest_rules = {}
    for row in rows:
        original = _row_key(row) + (row['source_hash'],)
        version = _rule_number(row)
        latest_rules[original] = max(version, latest_rules.get(original, version))

    values_by_receipt = {}
    for row in rows:
        key = _row_key(row)
        if _rule_number(row) != latest_rules[key + (row['source_hash'],)]:
            continue
        receipt = key + (row['received_at'],)
        value = _business_value(row)
        if receipt in values_by_receipt and values_by_receipt[receipt] != value:
            raise WeatherValueConflict('ambiguous_same_time_correction')
        values_by_receipt[receipt] = value
        yield key, row


def select_base_rounds(rows, source, categories, backfill_window):
    """마지막 완성 회차와 재수집 범위 안의 더 새로운 미완성 회차 보존."""
    if not rows:
        return []
    time_field = 'target_at' if source == 'odam' else 'issued_at'
    targets_by_round = {}
    for row in rows:
        targets = targets_by_round.setdefault(row[time_field], {})
        targets.setdefault(row['target_at'], set()).add(row['category'])

    times = {value: datetime.fromisoformat(value) for value in targets_by_round}
    round_times = sorted(targets_by_round, key=times.__getitem__, reverse=True)
    expected = set(categories)
    latest_complete = None
    for round_time in round_times:
        targets = targets_by_round[round_time]
        enough_targets = source == 'odam' or len(targets) == 6
        if enough_targets and all(expected <= arrived for arrived in targets.values()):
            latest_complete = round_time
            break

    # 같은 자료를 재처리할 때도 source별 최신 회차를 기준으로 보존 범위를 계산.
    cutoff = times[round_times[0]] - backfill_window
    keep_times = {
        value for value in round_times
        if times[value] >= cutoff
        and (latest_complete is None or times[value] > times[latest_complete])
    }
    if latest_complete is not None:
        # 기간 밖 마지막 완성 자료의 신선도는 조회 시 별도로 판정.
        keep_times.add(latest_complete)
    return [row for row in rows if row[time_field] in keep_times]


def latest_per_target(rows, target_limit):
    """최근 대상 시각만 남기고, 같은 대상 시각·항목은 최신 발표 행 하나 선택."""
    target_times = sorted({row['target_at'] for row in rows}, reverse=True)[:target_limit]
    latest = {}
    for row in rows:
        if row['target_at'] not in target_times:
            continue
        key = (row['target_at'], row['category'])
        previous = latest.get(key)
        if previous is None or newer(row, previous):
            latest[key] = row
    return list(latest.values())


def trim_rows(rows, *, backfill_hours=6):
    if type(backfill_hours) not in (int, float) or backfill_hours <= 0:
        raise ValueError('invalid_weather_backfill_hours')
    try:
        backfill_window = timedelta(hours=backfill_hours)
    except (ValueError, OverflowError) as error:
        raise ValueError('invalid_weather_backfill_hours') from error
    if backfill_window <= timedelta(0):
        raise ValueError('invalid_weather_backfill_hours')

    keep = []
    for source, categories in [('odam', OBS), ('vsrt', FORECAST)]:
        source_rows = [row for row in rows if row['source'] == source]
        keep.extend(select_base_rounds(source_rows, source, categories, backfill_window))

    # 예보 회차가 바뀌어도 현재 시각의 SKY를 조회할 수 있도록 최근 8개 대상 유지.
    sky_rows = [row for row in rows if row['source'] == 'vsrt' and row['category'] == 'SKY']
    kept_ids = {row['_id'] for row in keep}
    for row in latest_per_target(sky_rows, 8):
        if row['_id'] not in kept_ids:
            keep.append(row)

    # SNO와 KIM은 각각 최근 12개 대상 시각의 보완값 유지.
    for source in ('shrt', 'KIMG'):
        source_rows = [row for row in rows if row['source'] == source]
        keep.extend(latest_per_target(source_rows, 12))
    return keep


def merge_rows(previous, incoming, *, backfill_hours=6):
    """중복을 제거하고 보관 범위를 제한. 같은 수신 시각의 값 충돌은 거절."""
    by_key = {}
    for key, row in _checked_candidates(previous + incoming):
        saved = by_key.get(key)
        if saved is None or newer(row, saved):
            by_key[key] = row
    return trim_rows(list(by_key.values()), backfill_hours=backfill_hours)
