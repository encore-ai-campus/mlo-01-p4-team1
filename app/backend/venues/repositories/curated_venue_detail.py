"""Read-only detail for the curated venues in the configured local MCP catalog."""

from functools import lru_cache
import json
import os
from pathlib import Path

from django.conf import settings


@lru_cache(maxsize=2)
def _public_venues(path):
    try:
        catalog = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {row["venue_id"]: row for row in catalog["venues"] if "public_listing" in row}


def curated_venue_detail(venue_id):
    if not settings.LOCAL_DEMO or not venue_id.startswith("VN_MVP_"):
        return None
    path = os.environ.get("VENUE_MCP_CATALOG")
    venue = _public_venues(path).get(venue_id) if path else None
    if venue is None:
        return None
    return {
        "venue_id": venue_id,
        "venue_name": venue["name"],
        "category": venue["venue_category"],
        "sub_category": venue.get("sub_category"),
        "road_address": venue["address"],
        "dong_name": venue["admin_dong"],
        "curated_listing": {**venue["public_listing"], "budget_example": venue.get("budget_example")},
    }
