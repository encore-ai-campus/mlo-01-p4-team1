"""Sanitized observation emitter aligned with aws-infra logs-mlo.* dynamic=strict schema."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from django.utils import timezone

logger = logging.getLogger('backend.venues.observability')

ALLOWED_DATASETS = frozenset(('mlo.pipeline', 'mlo.ads', 'mlo.security', 'mlo.service'))
ALLOWED_STATES = frozenset((
    'QUEUED', 'STARTED', 'SENT', 'ACKNOWLEDGED', 'COMMITTED',
    'SEARCHABLE', 'PUBLISHED', 'RETRYING', 'FAILED', 'QUARANTINED',
    'SUCCEEDED', 'STALE', 'STOPPED', 'HEARTBEAT',
))
ALLOWED_OUTCOMES = frozenset(('success', 'failure', 'unknown'))
_SLUG_RE = re.compile(r'[^a-z0-9_.-]+')
_ERR_CODE_RE = re.compile(r'^[A-Z][A-Z0-9_]{1,79}$')
_TRACE_RE = re.compile(r'^[a-f0-9]{32}$')
_VALID_ACCOUNTS = frozenset(('encore1', 'encore2', 'encore3', 'encore4', 'encore5'))
_SPOOL_LOCK = threading.Lock()
_MAX_SPOOL_BYTES = 16 * 1024 * 1024


def _slug(value: str, fallback: str = 'unknown') -> str:
    cleaned = _SLUG_RE.sub('-', (value or '').strip().lower()).strip('-.')
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f'v-{cleaned}' if cleaned else fallback
    return cleaned[:80]


def _normalize_uuid(value: Optional[str]) -> str:
    if value:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            pass
    return str(uuid.uuid4())


def _normalize_trace_id(request_id: Optional[str]) -> Optional[str]:
    if not request_id:
        return None
    compact = str(request_id).replace('-', '').lower()
    return compact if _TRACE_RE.fullmatch(compact) else None


def build_sanitized_observation(
    *,
    dataset: str,
    action: str,
    outcome: str,
    pipeline_name: str,
    stage: str,
    state: str,
    attempt: int = 1,
    duration_ms: Optional[int] = None,
    run_id: Optional[str] = None,
    business_event_id: Optional[str] = None,
    release_id: Optional[str] = None,
    records_in: Optional[int] = None,
    records_out: Optional[int] = None,
    error_code: Optional[str] = None,
    retryable: bool = False,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a strict, PII-free observation envelope compatible with logs-mlo.* indices."""
    ds = dataset if dataset in ALLOWED_DATASETS else 'mlo.service'
    resolved_state = state if state in ALLOWED_STATES else 'STARTED'
    resolved_outcome = outcome if outcome in ALLOWED_OUTCOMES else 'unknown'
    if resolved_state in {'FAILED', 'QUARANTINED'}:
        resolved_outcome = 'failure'
    elif resolved_state in {'SEARCHABLE', 'PUBLISHED', 'SUCCEEDED'}:
        resolved_outcome = 'success'

    host_name = _slug(socket.gethostname() or 'local-control', 'local-control')
    account_name = os.environ.get('MLO_CLOUD_ACCOUNT', 'encore1').strip().lower()
    if account_name not in _VALID_ACCOUNTS:
        account_name = 'encore1'

    event_obj: Dict[str, Any] = {
        'id': str(uuid.uuid4()),
        'dataset': ds,
        'action': _slug(action, 'venue-action'),
        'outcome': resolved_outcome,
    }
    if duration_ms is not None and isinstance(duration_ms, int) and duration_ms >= 0:
        event_obj['duration'] = duration_ms

    pipeline_obj: Dict[str, Any] = {
        'name': _slug(pipeline_name, 'venues-catalog'),
        'run_id': _normalize_uuid(run_id),
        'stage': _slug(stage, 'api'),
        'state': resolved_state,
        'attempt': max(1, int(attempt)),
    }
    if release_id:
        pipeline_obj['release_id'] = _slug(release_id, 'local')
    if business_event_id:
        pipeline_obj['business_event_id'] = _normalize_uuid(business_event_id)
    if records_in is not None and isinstance(records_in, int) and records_in >= 0:
        pipeline_obj['records_in'] = records_in
    if records_out is not None and isinstance(records_out, int) and records_out >= 0:
        pipeline_obj['records_out'] = records_out

    envelope: Dict[str, Any] = {
        '@timestamp': timezone.now().isoformat().replace('+00:00', 'Z'),
        'event': event_obj,
        'service': {'name': _slug(os.environ.get('MLO_SERVICE_NAME', 'venues-web'), 'venues-web')},
        'host': {'name': host_name},
        'pipeline': pipeline_obj,
        'cloud': {
            'provider': 'aws',
            'region': 'ap-northeast-2',
            'account': {'name': account_name},
        },
    }

    trace_id = _normalize_trace_id(request_id)
    if trace_id:
        envelope['trace'] = {'id': trace_id}

    if error_code or resolved_state in {'FAILED', 'QUARANTINED'}:
        normalized_code = re.sub(r'[^A-Z0-9_]+', '_', (error_code or 'UNEXPECTED_ERROR').upper()).strip('_')
        if not _ERR_CODE_RE.fullmatch(normalized_code):
            normalized_code = 'VENUE_ERROR'
        envelope['error'] = {'code': normalized_code[:80], 'retryable': bool(retryable)}

    return envelope


def emit_observation(**kwargs: Any) -> Dict[str, Any]:
    """Log and optionally spool a sanitized NDJSON observation without interrupting callers."""
    envelope = build_sanitized_observation(**kwargs)
    serialized = json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))
    if len(serialized.encode('utf-8')) > 4096:
        envelope['pipeline'].pop('release_id', None)
        serialized = json.dumps(envelope, ensure_ascii=False, separators=(',', ':'))

    logger.info(serialized)

    spool_path_raw = os.environ.get('VENUE_OBSERVATION_SPOOL_PATH', '').strip()
    if spool_path_raw:
        try:
            spool_path = Path(spool_path_raw)
            spool_path.parent.mkdir(parents=True, exist_ok=True)
            with _SPOOL_LOCK:
                if spool_path.exists() and spool_path.stat().st_size >= _MAX_SPOOL_BYTES:
                    logger.warning('[ObservationSpool] Backpressure cap reached; skipping spool write')
                else:
                    with spool_path.open('a', encoding='utf-8') as handle:
                        handle.write(serialized + '\n')
        except OSError:
            logger.warning('[ObservationSpool] Failed writing sanitized NDJSON line')

    return envelope
