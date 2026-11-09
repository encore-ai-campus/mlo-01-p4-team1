"""Authenticated reads of materialized V2 snapshots; never scan source events here."""

from datetime import date

from ads_analytics_v2.adapters.mongo import MongoStore
from ads_analytics_v2.errors import AnalyticsError
from ads_analytics_v2.events import stamp, timestamp
from ads_analytics_v2.reporting import COUNTERS, REPORT_POLICY, validate_query
from django.conf import settings
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from pymongo import MongoClient
from rest_framework.response import Response

from api.ads import AdsAdminView, query_filters
from domains.ads.errors import DomainError


def latest_snapshot(cutoff):
    if not settings.ADS_V2_SINK_MONGODB_URI or not settings.ADS_V2_SINK_MONGODB_DATABASE:
        raise DomainError("ANALYTICS_UNAVAILABLE", "광고 성과 저장소 설정이 필요합니다.", 503)
    client = MongoClient(
        settings.ADS_V2_SINK_MONGODB_URI,
        tz_aware=True,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
        socketTimeoutMS=5000,
    )
    try:
        return MongoStore(client[settings.ADS_V2_SINK_MONGODB_DATABASE]).latest_report(
            cutoff=cutoff
        )
    finally:
        client.close()


def response_data(snapshot, cutoff, scope, now):
    base = {
        "report_policy_version": REPORT_POLICY,
        "policy_confirmed": False,
        "scope": scope,
        "as_of": None,
        "report_id": None,
        "source_snapshot": None,
        "aggregation_interval_seconds": settings.ADS_V2_INTERVAL_SECONDS,
        "rows": [],
        "is_stale": True,
    }
    if snapshot is None:
        return {
            **base,
            "report_available": False,
            "status": "PENDING",
            "message": "광고 성과 집계 스냅샷을 기다리고 있습니다.",
            "totals": dict.fromkeys((*COUNTERS, "ctr_percent"), None),
        }
    # Filtering an already filtered snapshot could silently conceal missing data.
    if snapshot.get("scope") != {} or snapshot.get("status") != "PROVISIONAL":
        raise ValueError("invalid_full_snapshot")
    as_of = timestamp(snapshot["as_of"])
    if as_of > cutoff:
        raise ValueError("snapshot_after_cutoff")
    source_rows = snapshot["rows"]
    if not isinstance(source_rows, list) or len(source_rows) > 5000:
        raise ValueError("invalid_snapshot_rows")
    rows = []
    for original in source_rows:
        day = date.fromisoformat(original["date_kst"]).isoformat()
        if any(type(original.get(name)) is not int or original[name] < 0 for name in COUNTERS):
            raise ValueError("invalid_snapshot_counters")
        if any(
            scope.get(key) and original.get(key) != scope[key]
            for key in ("ad_id", "placement", "media_id")
        ):
            continue
        if scope.get("date_from") and not scope["date_from"] <= day <= scope["date_to"]:
            continue
        row = {
            key: original[key] for key in ("date_kst", "ad_id", "placement", "media_id", *COUNTERS)
        }
        row["ctr_percent"] = (
            round(row["matched_clicks"] * 100 / row["impressions"], 6)
            if row["impressions"]
            else None
        )
        rows.append(row)
    totals = {name: sum(row[name] for row in rows) for name in COUNTERS}
    totals["ctr_percent"] = (
        round(totals["matched_clicks"] * 100 / totals["impressions"], 6)
        if totals["impressions"]
        else None
    )
    return {
        **base,
        "report_available": True,
        "status": "PROVISIONAL",
        "report_policy_version": snapshot["report_policy_version"],
        "report_id": snapshot["report_id"],
        "as_of": stamp(as_of),
        "source_snapshot": {"report_id": snapshot["report_id"], "as_of": stamp(as_of), "scope": {}},
        "is_stale": (now - as_of).total_seconds() > settings.ADS_V2_STALE_SECONDS,
        "message": "제안 정책에 따른 잠정 성과입니다. 수집 완료·확정 실적을 뜻하지 않습니다.",
        "rows": rows,
        "totals": totals,
    }


class AdsAnalyticsView(AdsAdminView):
    http_method_names = ["get", "head", "options"]

    @extend_schema(
        tags=["ads-admin"],
        responses={200: OpenApiTypes.OBJECT},
        parameters=[
            OpenApiParameter(name=name, type=str)
            for name in ("date_from", "date_to", "ad_id", "placement", "media_id", "cutoff")
        ],
        description=(
            "기존 ADMIN 권한으로 저장된 전체 성과 스냅샷을 필터링합니다. "
            "원본 이벤트를 재집계하지 않습니다."
        ),
    )
    def get(self, request):
        allowed = {"date_from", "date_to", "ad_id", "placement", "media_id", "cutoff"}
        query_filters(request, allowed)
        if any(not value for value in request.query_params.values()):
            raise DomainError("VALIDATION_ERROR", "비어 있는 조회 필드는 허용하지 않습니다.", 400)
        now = timezone.now()
        values = request.query_params.dict()
        raw_cutoff = values.pop("cutoff", stamp(now))
        try:
            cutoff, _, scope = validate_query(raw_cutoff, **values)
        except (AnalyticsError, ValueError, TypeError):
            raise DomainError(
                "VALIDATION_ERROR", "조회 기간·시각·필터를 확인해 주세요.", 400
            ) from None
        try:
            snapshot = latest_snapshot(cutoff) if settings.ADS_V2_ENABLED else None
            result = response_data(snapshot, cutoff, scope, now)
        except DomainError:
            raise
        except Exception:
            raise DomainError(
                "ANALYTICS_UNAVAILABLE", "광고 성과 스냅샷을 확인할 수 없습니다.", 503
            ) from None
        result["enabled"] = settings.ADS_V2_ENABLED
        return Response(result)
