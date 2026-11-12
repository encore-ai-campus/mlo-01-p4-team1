"""Pure-Python Bronze/Silver/Gold transformations for Track 2 venues-catalog lakehouse."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

VALID_CLOSURE_STATES = frozenset(('OPEN', 'CLOSURE_REPORTED', 'CLOSED_HIDDEN'))
DEFAULT_DATASET_VERSION = 'v1'


def canonical_digest(payload: Any) -> str:
    """Compute a deterministic SHA-256 digest of JSON-serializable catalog payloads."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
        return num
    except (TypeError, ValueError):
        return default


def normalize_venue_record(
    raw: Dict[str, Any],
    *,
    dataset_version: str = DEFAULT_DATASET_VERSION,
    ingested_at: str | None = None,
) -> Dict[str, Any]:
    """Normalize a raw venue export row into Silver-compatible canonical fields."""
    venue_id = str(raw.get('venue_id') or raw.get('id') or '').strip()
    if not venue_id:
        raise ValueError('venue_id_required')

    status_code = str(raw.get('status_code') or raw.get('status') or 'OPEN').strip().upper()
    if status_code not in VALID_CLOSURE_STATES:
        status_code = 'OPEN'

    category = str(raw.get('category') or 'CAFE').strip().upper()
    store_type = str(raw.get('store_type') or 'LOCAL').strip().upper()
    revision = max(1, _safe_int(raw.get('revision') or raw.get('version'), 1))
    price_tier = max(1, min(5, _safe_int(raw.get('price_tier'), 2)))
    avg_price = max(0, _safe_int(raw.get('avg_price'), 0))
    menus = raw.get('menus') if isinstance(raw.get('menus'), list) else []
    signature_menus = [
        str(item.get('menu_name') or '').strip()
        for item in menus
        if isinstance(item, dict) and item.get('is_signature') and str(item.get('menu_name') or '').strip()
    ][:5]

    timestamp = str(ingested_at or raw.get('exported_at') or datetime.now(timezone.utc).isoformat())
    updated_at = str(raw.get('updated_at') or timestamp)
    is_searchable = status_code != 'CLOSED_HIDDEN'

    normalized = {
        'venue_id': venue_id,
        'dataset_version': str(dataset_version or DEFAULT_DATASET_VERSION).strip() or DEFAULT_DATASET_VERSION,
        'venue_name': str(raw.get('venue_name') or '').strip(),
        'gu_name': str(raw.get('gu_name') or '').strip(),
        'dong_name': str(raw.get('dong_name') or '').strip(),
        'road_address': str(raw.get('road_address') or '').strip(),
        'category': category,
        'sub_category': str(raw.get('sub_category') or '').strip(),
        'store_type': store_type,
        'brand_name': str(raw.get('brand_name') or '').strip(),
        'price_tier': price_tier,
        'avg_price': avg_price,
        'latitude': _safe_float(raw.get('latitude'), 37.5),
        'longitude': _safe_float(raw.get('longitude'), 127.0),
        'is_partner': bool(raw.get('is_partner', False)),
        'is_open_now': bool(raw.get('is_open_now', True)),
        'primary_weather_affinity': str(raw.get('primary_weather_affinity') or 'ALL').strip().upper(),
        'status_code': status_code,
        'is_searchable': is_searchable,
        'deleted': not is_searchable,
        'revision': revision,
        'menu_count': _safe_int(raw.get('menu_count'), len(menus)),
        'signature_menus': signature_menus,
        'updated_at': updated_at,
        'ingested_at': timestamp,
    }
    normalized['source_sha256'] = str(raw.get('source_sha256') or '').strip() or canonical_digest(normalized)
    return normalized


def deduplicate_latest_silver(
    raw_records: Iterable[Dict[str, Any]],
    *,
    dataset_version: str = DEFAULT_DATASET_VERSION,
) -> List[Dict[str, Any]]:
    """Keep the latest deterministic record per venue_id using (revision, updated_at)."""
    latest_by_id: Dict[str, Dict[str, Any]] = {}
    for raw in raw_records:
        item = normalize_venue_record(raw, dataset_version=dataset_version)
        vid = item['venue_id']
        prev = latest_by_id.get(vid)
        if prev is None or (item['revision'], item['updated_at']) >= (prev['revision'], prev['updated_at']):
            latest_by_id[vid] = item
    return [latest_by_id[k] for k in sorted(latest_by_id)]


def project_gold_records(silver_records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Project Silver records into searchable Gold catalog items and district aggregates."""
    searchable_venues: List[Dict[str, Any]] = []
    by_gu: Dict[str, Dict[str, Any]] = {}

    for rec in silver_records:
        gu = str(rec.get('gu_name') or '미지정')
        bucket = by_gu.setdefault(
            gu,
            {
                'gu_name': gu,
                'total_venues': 0,
                'searchable_venues': 0,
                'hidden_venues': 0,
                'cafe_count': 0,
                'pub_count': 0,
                'partner_count': 0,
                '_price_sum': 0,
                '_price_count': 0,
            },
        )
        bucket['total_venues'] += 1
        if rec.get('is_searchable'):
            bucket['searchable_venues'] += 1
            searchable_venues.append(rec)
            if rec.get('category') == 'PUB':
                bucket['pub_count'] += 1
            else:
                bucket['cafe_count'] += 1
            if rec.get('is_partner'):
                bucket['partner_count'] += 1
            avg_price = _safe_int(rec.get('avg_price'), 0)
            if avg_price > 0:
                bucket['_price_sum'] += avg_price
                bucket['_price_count'] += 1
        else:
            bucket['hidden_venues'] += 1

    gu_summaries: List[Dict[str, Any]] = []
    for gu in sorted(by_gu):
        b = by_gu[gu]
        avg_price_krw = round(b['_price_sum'] / b['_price_count']) if b['_price_count'] else 0
        gu_summaries.append({
            'gu_name': b['gu_name'],
            'total_venues': b['total_venues'],
            'searchable_venues': b['searchable_venues'],
            'hidden_venues': b['hidden_venues'],
            'cafe_count': b['cafe_count'],
            'pub_count': b['pub_count'],
            'partner_count': b['partner_count'],
            'avg_price_krw': avg_price_krw,
        })

    return {
        'venues': searchable_venues,
        'gu_summaries': gu_summaries,
    }
