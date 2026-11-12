"""DB catalog mirroring and Track 2 S3 raw snapshot export for venues-catalog."""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from django.db import DatabaseError, transaction

from backend.venues.models.catalog import VenueCatalogRecord
from backend.venues.models.outbox import VenueOutbox
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.streaming.observability import emit_observation
from pipeline.venues.catalog_normalize import (
    DEFAULT_DATASET_VERSION,
    canonical_digest,
    normalize_venue_record,
)

_SAFE_DATASET_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')


def resolved_dataset_version(dataset_version: Optional[str] = None) -> str:
    candidate = (dataset_version or os.environ.get('VENUE_DATASET_VERSION') or DEFAULT_DATASET_VERSION).strip().lower()
    if not _SAFE_DATASET_RE.fullmatch(candidate):
        raise ValueError('dataset_version은 영문 소문자·숫자·하이픈 1~63자여야 합니다.')
    return candidate


def _target_gus(repo, gus: Optional[Iterable[str]]) -> List[str]:
    if gus:
        return [str(g).strip() for g in gus if str(g).strip()]
    return list(repo.get_all_available_gus())


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_catalog_model(venue: Dict[str, Any], menus: List[Dict[str, Any]], dataset_version: str) -> VenueCatalogRecord:
    payload = dict(venue)
    source_sha256 = canonical_digest({'venue': payload, 'menus': menus})
    return VenueCatalogRecord(
        venue_id=str(payload['venue_id']),
        dataset_version=dataset_version,
        venue_name=str(payload.get('venue_name') or '')[:200],
        gu_name=str(payload.get('gu_name') or '')[:50],
        dong_name=str(payload.get('dong_name') or '')[:80],
        road_address=str(payload.get('road_address') or '')[:255],
        category=str(payload.get('category') or 'CAFE').strip().upper()[:20],
        sub_category=str(payload.get('sub_category') or '')[:100],
        store_type=str(payload.get('store_type') or 'LOCAL').strip().upper()[:20],
        brand_name=str(payload.get('brand_name') or '')[:100],
        price_tier=max(1, min(5, _safe_int(payload.get('price_tier'), 2))),
        avg_price=max(0, _safe_int(payload.get('avg_price'), 0)),
        latitude=_safe_float(payload.get('latitude'), 37.5),
        longitude=_safe_float(payload.get('longitude'), 127.0),
        is_partner=bool(payload.get('is_partner', False)),
        is_open_now=bool(payload.get('is_open_now', True)),
        primary_weather_affinity=str(payload.get('primary_weather_affinity') or 'ALL')[:32],
        venue_payload=payload,
        menus_payload=menus,
        source_sha256=source_sha256,
    )


def _upsert_batch(records: List[VenueCatalogRecord]) -> tuple[int, int]:
    if not records:
        return 0, 0
    ids = [r.venue_id for r in records]
    existing_ids = set(VenueCatalogRecord.objects.filter(venue_id__in=ids).values_list('venue_id', flat=True))
    to_create = [r for r in records if r.venue_id not in existing_ids]
    to_update = [r for r in records if r.venue_id in existing_ids]

    update_fields = [
        'dataset_version', 'venue_name', 'gu_name', 'dong_name', 'road_address',
        'category', 'sub_category', 'store_type', 'brand_name', 'price_tier',
        'avg_price', 'latitude', 'longitude', 'is_partner', 'is_open_now',
        'primary_weather_affinity', 'venue_payload', 'menus_payload', 'source_sha256',
    ]
    with transaction.atomic():
        if to_create:
            VenueCatalogRecord.objects.bulk_create(to_create, batch_size=500)
        if to_update:
            VenueCatalogRecord.objects.bulk_update(to_update, update_fields, batch_size=500)
    return len(to_create), len(to_update)


def sync_catalog_to_db(
    *,
    gus: Optional[Iterable[str]] = None,
    dataset_version: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Synchronize active CSV catalog venues and menus into VenueCatalogRecord idempotently."""
    from backend.venues.repositories.csv_lazy_repo import get_venue_repo

    version = resolved_dataset_version(dataset_version)
    repo = get_venue_repo()
    regions = _target_gus(repo, gus)

    seen_ids = set()
    created_total = 0
    updated_total = 0
    scanned_total = 0
    processed_regions = 0

    for gu in regions:
        venues = repo.get_venues_by_gu(gu)
        if not venues:
            continue
        processed_regions += 1
        batch: List[VenueCatalogRecord] = []
        for venue in venues:
            vid = str(venue.get('venue_id') or '').strip()
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            menus = list(repo.get_menus_by_venue(vid))
            batch.append(_build_catalog_model(venue, menus, version))
            scanned_total += 1
            if limit is not None and scanned_total >= max(1, int(limit)):
                break
            if len(batch) >= 500:
                if not dry_run:
                    c, u = _upsert_batch(batch)
                    created_total += c
                    updated_total += u
                batch = []
        if batch and not dry_run:
            c, u = _upsert_batch(batch)
            created_total += c
            updated_total += u
        if limit is not None and scanned_total >= max(1, int(limit)):
            break

    emit_observation(
        dataset='mlo.pipeline',
        action='catalog-db-sync',
        outcome='success',
        pipeline_name='venues-catalog',
        stage='db-sync',
        state='SUCCEEDED' if not dry_run else 'STARTED',
        records_in=scanned_total,
        records_out=created_total + updated_total,
    )
    return {
        'dry_run': bool(dry_run),
        'dataset_version': version,
        'regions_processed': processed_regions,
        'venues_scanned': scanned_total,
        'created': created_total,
        'updated': updated_total,
    }


def _write_ndjson_atomic(path: Path, rows: List[Dict[str, Any]]) -> str:
    content = '\n'.join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    if content:
        content += '\n'
    data = content.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return canonical_digest(content) if not rows else __import__('hashlib').sha256(data).hexdigest()


def _upload_directory_to_s3(files: Dict[str, Path], bucket: str, prefix: str) -> List[str]:
    import boto3
    from botocore.config import Config

    region = os.environ.get('VENUE_S3_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'ap-northeast-2'
    client = boto3.client('s3', region_name=region, config=Config(connect_timeout=5, read_timeout=15, retries={'max_attempts': 2}))
    uploaded: List[str] = []
    clean_prefix = prefix.strip('/') + '/'
    for filename, local_path in files.items():
        key = clean_prefix + filename
        client.upload_file(str(local_path), bucket, key)
        uploaded.append(f's3://{bucket}/{key}')
    return uploaded


def export_lakehouse_snapshot(
    *,
    gus: Optional[Iterable[str]] = None,
    dataset_version: Optional[str] = None,
    output_dir: Optional[str | Path] = None,
    s3_bucket: Optional[str] = None,
    include_outbox: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Export Track 2 immutable raw/venues/<dataset_version>/ snapshot and optionally upload to S3."""
    from backend.venues.repositories.csv_lazy_repo import get_venue_repo

    version = resolved_dataset_version(dataset_version)
    repo = get_venue_repo()
    regions = _target_gus(repo, gus)
    bucket = (s3_bucket or os.environ.get('VENUE_RAW_S3_BUCKET', '')).strip()
    now_iso = datetime.now(timezone.utc).isoformat()

    overlays_by_id: Dict[str, VenueStatusOverlay] = {
        row.venue_id: row for row in VenueStatusOverlay.objects.all()
    }
    catalog_rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for gu in regions:
        for venue in repo.get_venues_by_gu(gu):
            vid = str(venue.get('venue_id') or '').strip()
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            overlay = overlays_by_id.get(vid)
            menus = list(repo.get_menus_by_venue(vid))
            enriched = {
                **venue,
                'menus': menus,
                'menu_count': len(menus),
                'status_code': overlay.status if overlay else 'OPEN',
                'revision': overlay.version if overlay else 1,
                'updated_at': overlay.updated_at.isoformat() if overlay and overlay.updated_at else now_iso,
                'exported_at': now_iso,
            }
            catalog_rows.append(normalize_venue_record(enriched, dataset_version=version, ingested_at=now_iso))

    outbox_rows: List[Dict[str, Any]] = []
    if include_outbox:
        for row in VenueOutbox.objects.order_by('occurred_at', 'id'):
            outbox_rows.append({
                'event_id': row.event_id,
                'event_type': row.event_type,
                'venue_id': row.venue_id,
                'request_id': row.request_id,
                'state': row.state,
                'payload': row.payload if isinstance(row.payload, dict) else {},
                'occurred_at': row.occurred_at.isoformat() if row.occurred_at else None,
            })

    prefix = f'raw/venues/{version}/'
    written_files: Dict[str, str] = {}
    uploaded_uris: List[str] = []
    target_path = None

    if not dry_run:
        if output_dir:
            base = Path(output_dir) / 'raw' / 'venues' / version
            cleanup_ctx = None
        else:
            cleanup_ctx = tempfile.TemporaryDirectory(prefix='venues-raw-')
            base = Path(cleanup_ctx.name) / 'raw' / 'venues' / version
        try:
            target_path = str(base)
            catalog_file = base / 'venues_catalog.ndjson'
            outbox_file = base / 'venues_outbox.ndjson'
            manifest_file = base / 'manifest.json'

            file_hashes = {'venues_catalog.ndjson': _write_ndjson_atomic(catalog_file, catalog_rows)}
            export_map: Dict[str, Path] = {'venues_catalog.ndjson': catalog_file}
            if include_outbox:
                file_hashes['venues_outbox.ndjson'] = _write_ndjson_atomic(outbox_file, outbox_rows)
                export_map['venues_outbox.ndjson'] = outbox_file

            manifest = {
                'version': 'track2-raw-venues/1',
                'query': 'venues-catalog',
                'dataset_version': version,
                'raw_prefix': prefix,
                'lake_prefix': f'lake/venues/{version}/v1/',
                'exported_at': now_iso,
                'complete': True,
                'records': {
                    'venues': len(catalog_rows),
                    'outbox_events': len(outbox_rows),
                },
                'files': file_hashes,
                'schema_sha256': canonical_digest({
                    'version': 'track2-raw-venues/1',
                    'layers': ['bronze', 'silver', 'gold'],
                    'primary_key': 'venue_id',
                }),
            }
            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_bytes(manifest_bytes)
            export_map['manifest.json'] = manifest_file
            written_files = {name: str(p) for name, p in export_map.items()}

            if bucket:
                uploaded_uris = _upload_directory_to_s3(export_map, bucket, prefix)
        finally:
            if cleanup_ctx is not None:
                cleanup_ctx.cleanup()
                if not output_dir:
                    target_path = None
                    written_files = {}

    emit_observation(
        dataset='mlo.pipeline',
        action='lakehouse-raw-export',
        outcome='success',
        pipeline_name='venues-catalog',
        stage='raw-export',
        state='PUBLISHED' if uploaded_uris or (not dry_run and bool(output_dir)) else 'STARTED',
        records_in=len(catalog_rows),
        records_out=len(catalog_rows),
    )
    return {
        'dry_run': bool(dry_run),
        'dataset_version': version,
        'raw_prefix': prefix,
        'lake_prefix': f'lake/venues/{version}/v1/',
        'venues_exported': len(catalog_rows),
        'outbox_exported': len(outbox_rows),
        'output_dir': target_path,
        'files': written_files,
        's3_uploaded': uploaded_uris,
    }


def get_lakehouse_runtime_status() -> Dict[str, Any]:
    """Return safe database-mirror and S3/Spark lakehouse readiness metrics."""
    try:
        db_count = VenueCatalogRecord.objects.count()
        db_regions = VenueCatalogRecord.objects.values('gu_name').distinct().count()
    except (DatabaseError, AssertionError, Exception):
        db_count = 0
        db_regions = 0

    dataset_version = (os.environ.get('VENUE_DATASET_VERSION') or DEFAULT_DATASET_VERSION).strip() or DEFAULT_DATASET_VERSION
    return {
        'dataset_version': dataset_version,
        'db_synced_venues_count': db_count,
        'db_synced_regions_count': db_regions,
        's3_raw_bucket_configured': bool(os.environ.get('VENUE_RAW_S3_BUCKET', '').strip()),
        's3_lake_bucket_configured': bool(os.environ.get('VENUE_LAKE_S3_BUCKET', '').strip()),
        'spark_job_module': 'pipeline.venues.spark_job',
        'raw_prefix': f'raw/venues/{dataset_version}/',
        'lake_prefix': f'lake/venues/{dataset_version}/v1/',
    }
