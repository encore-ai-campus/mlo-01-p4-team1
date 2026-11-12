"""Read-only detail for the curated venues in the configured local MCP catalog."""

from functools import lru_cache
import json
import os
from pathlib import Path

from django.conf import settings


def configured_sqlite_catalog():
    path = os.environ.get("VENUE_MCP_PUBLIC_CATALOG", "").strip()
    return bool(path and Path(path).suffix.lower() in {".sqlite", ".sqlite3"})


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


def local_catalog_detail(venue_id):
    """Display the same snapshot as MCP, only in the explicitly local demo.

    A snapshot's public flag is not evidence of current business hours, menu
    availability, or a verified price. Keep those fields out of the response.
    """
    if not settings.LOCAL_DEMO:
        return None
    path = os.environ.get("VENUE_MCP_PUBLIC_CATALOG")
    if not path or configured_sqlite_catalog():
        return None
    try:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict) or type(snapshot.get("preview_only")) is not bool:
            return None
        rows = snapshot.get("venues")
        if not isinstance(rows, list):
            return None
        matches = [row for row in rows if isinstance(row, dict) and row.get("venue_id") == venue_id]
        if not matches:
            return None
        if len(matches) != 1:
            return {"_catalog_blocked": True}
        venue = matches[0]
        if venue.get("is_public") is not True or venue.get("venue_category") not in ("CAFE", "PUB"):
            return {"_catalog_blocked": True}
        if any(not isinstance(venue.get(field), str) or not venue[field].strip()
               for field in ("name", "address", "admin_dong")):
            return {"_catalog_blocked": True}
        research = venue.get("research")
        if research is not None:
            if not isinstance(research, dict):
                return {"_catalog_blocked": True}
            research = {key: value for key, value in research.items()
                        if key in ("description", "seating", "limitations", "checked_on", "representative_menus")
                        and isinstance(value, str)} | {
                            "sources": [source for source in research.get("sources", []) if isinstance(source, str)]
                            if isinstance(research.get("sources", []), list) else []
                        }
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return {
        "venue_id": venue_id, "venue_name": venue["name"], "category": venue["venue_category"],
        "sub_category": venue.get("sub_category") if isinstance(venue.get("sub_category"), str) else None,
        "road_address": venue["address"], "dong_name": venue["admin_dong"],
        "research": research,
        "curated_listing": {"preview_only": True, "moods": [], "representative_menus": [],
                            "seat_summary": "", "checked_on": "", "registry_url": "", "budget_example": None},
    }
