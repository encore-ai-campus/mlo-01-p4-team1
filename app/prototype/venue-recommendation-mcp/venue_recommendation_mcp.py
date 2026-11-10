"""손님용 v0.2 매장 추천 MCP. 기존 점주 문맥과 별도 stdio 서버로 실행합니다."""

import argparse
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from venue_forecast import fetch_forecasts, project_endpoint, timestamp
from verify_tag_weather import SEAT_LABELS, normalize, read_tags, recommend, seat_result, verify_pilot


ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "실매장_태그_날씨_시범_v0.2.json"
DEFAULT_LOCATIONS = ROOT / "venue_weather_regions_v0.2.json"
TAG_FILE = ROOT / "공통_태그_목록_v0.2.csv"
TagValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Outdoor = Literal["ANY", "PREFER_OUTDOOR", "REQUIRE_OUTDOOR"]


class MenuOrder(BaseModel):
    """한 메뉴가 충족해야 하는 요리 계열과 유형입니다."""

    model_config = ConfigDict(extra="forbid")
    cuisine: TagValue | None = None
    types: Annotated[list[TagValue], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_tag(self):
        if not self.cuisine and not self.types:
            raise ValueError("주문 조건마다 요리 계열이나 메뉴 유형을 하나 이상 선택해 주세요.")
        return self


def region_venues(region, venues, locations):
    region_key = normalize(region)
    if region_key in {"서울", "서울시", "서울특별시"}:
        return venues

    selected = []
    for venue in venues:
        location = locations.get(venue["venue_id"], {})
        district = venue["district"]
        dong = venue.get("admin_dong") or location.get("admin_dong")
        if region_key == "성수" and district == "성동구" and (dong or "").startswith("성수"):
            selected.append(venue)
            continue
        address = {district}
        if dong:
            address.update({dong, f"{district} {dong}"})
        valid_names = {normalize(prefix + name) for prefix in ("", "서울 ", "서울특별시 ") for name in address}
        if region_key in valid_names:
            selected.append(venue)
    if not selected:
        raise ValueError("지원 지역을 확인해 주세요. 예를 들어 강남구, 논현2동, 성수1가2동입니다.")
    return selected


def registered_locations(path, venues):
    data = json.loads(path.read_text(encoding="utf-8"))
    locations = data["locations"]
    for venue in venues:
        location = locations.get(venue["venue_id"])
        if location is None:
            continue
        if (
            location["district"] != venue["district"]
            or (venue.get("admin_dong") and venue["admin_dong"] != location["admin_dong"])
            or type(location["nx"]) is not int or type(location["ny"]) is not int
            or not location["region_id"].startswith("region:")
        ):
            raise ValueError(f"매장 지역과 날씨 매핑이 일치하지 않습니다. {venue['venue_id']}")
    return data["mapping_version"], locations


def create_server(catalog_path=DEFAULT_CATALOG, locations_path=DEFAULT_LOCATIONS, *,
                  weather_base_url=None, demo_open_meteo=False):
    tags, labels, ancestors = read_tags(TAG_FILE)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    verify_pilot(catalog, tags)
    mapping_version, locations = registered_locations(locations_path, catalog["venues"])
    project_url = project_endpoint(weather_base_url) if weather_base_url else None
    catalog_source = "PUBLIC_SOURCE_PILOT" if catalog_path.resolve() == DEFAULT_CATALOG else "CONFIGURED_PUBLIC_CATALOG"
    venues = {row["venue_id"]: row for row in catalog["venues"]}
    menus = {row["menu_id"]: row for row in catalog["menus"]}
    server = FastMCP("venue-recommendation")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @server.tool(annotations=read_only)
    def list_venue_tags(kind: Literal["MOOD", "CUISINE", "MENU_TYPE"] | None = None) -> dict[str, Any]:
        """점주와 손님이 함께 쓰는 분위기·요리 계열·메뉴 유형 및 시범 지역을 조회합니다."""
        available_regions = {"서울", *[row["district"] for row in venues.values()]}
        available_regions.update(
            row["admin_dong"] for row in venues.values() if row.get("admin_dong")
        )
        available_regions.update(
            loc["admin_dong"] for venue_id, loc in locations.items()
            if venue_id in venues and loc.get("admin_dong")
        )
        return {
            "tag_version": "0.2",
            "regions": sorted(available_regions),
            "tags": [
                {
                    "id": tag["tag_id"], "kind": tag["kind"], "label": tag["label_ko"],
                    "parent_id": tag["parent_tag_id"] or None,
                    "aliases": [alias for alias in tag["aliases"].split("|") if alias],
                }
                for tag in tags.values() if kind is None or tag["kind"] == kind
            ],
        }

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                             idempotentHint=True, openWorldHint=True))
    async def recommend_venues(
        region: Annotated[TagValue, Field(description="서울, 자치구, 행정동 또는 성수")],
        visit_at: Annotated[TagValue, Field(description="UTC 오프셋이 포함된 ISO 8601 방문 시각")],
        preferred_moods: Annotated[list[TagValue], Field(max_length=12)] | None = None,
        orders: Annotated[list[MenuOrder], Field(max_length=8)] | None = None,
        outdoor: Outdoor = "ANY",
        limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
    ) -> dict[str, Any]:
        """등록 태그를 매칭하고 방문 시각의 예보로 야외 운영을 판정합니다. 일반 영업시간을 전제로 합니다."""
        visit = timestamp(visit_at)
        scoped_venues = region_venues(region, catalog["venues"], locations)
        if outdoor == "REQUIRE_OUTDOOR":
            scoped_venues = [row for row in scoped_venues if row["seats"]["outdoor"] == "CONFIRMED"]

        query = {
            "district": None, "preferred_moods": preferred_moods or [],
            "orders": [item.model_dump() for item in orders or []],
            "seating": "ANY", "weather": "NO_FORECAST",
        }
        # v0.2 is the single source for tag ancestry and the same-menu matching rule.
        base = recommend({**catalog, "venues": scoped_venues}, query, labels, ancestors)["ranked"]
        by_venue = await fetch_forecasts(
            {row["venue_id"]: locations.get(row["venue_id"]) for row in base}, visit,
            project_url=project_url, demo_open_meteo=demo_open_meteo,
        )

        decisions = {"ranked": [], "pending": [], "excluded": []}
        for matched in base:
            venue_id = matched["venue_id"]
            venue = venues[venue_id]
            forecast = by_venue[venue_id]
            bucket, seat, outdoor_priority = seat_result(venue["seats"], outdoor, forecast.seating_weather)
            if bucket is None:
                continue

            location = locations.get(venue_id, {})
            result = {
                "venue_id": venue_id, "name": venue["name"], "address": venue["address"],
                "district": venue["district"], "admin_dong": venue.get("admin_dong") or location.get("admin_dong"),
                "matched_moods": [{"id": tag_id, "label": tags[tag_id]["label_ko"]}
                                  for tag_id in matched["matched_mood_ids"]],
                "matched_orders": [
                    [{"menu_id": menu_id, "name": menus[menu_id]["name"],
                      "cuisine_tag_ids": menus[menu_id]["cuisine_tag_ids"],
                      "menu_type_tag_ids": menus[menu_id]["menu_type_tag_ids"]}
                     for menu_id in group]
                    for group in matched["matched_menu_groups"]
                ],
                "seat": {"code": seat, "label": SEAT_LABELS[seat]},
                "forecast": {**asdict(forecast), "region_id": location.get("region_id"),
                             "grid": [location["nx"], location["ny"]] if location else None},
            }
            rank = (-len(result["matched_moods"]), -outdoor_priority, venue_id)
            decisions[bucket].append((rank, result))

        counts = {bucket: len(rows) for bucket, rows in decisions.items()}
        forecast_count = sum(value.state != "NO_FORECAST" for value in by_venue.values())
        if not by_venue:
            forecast_status = "NOT_NEEDED"
        elif forecast_count == len(by_venue):
            forecast_status = "AVAILABLE"
        elif forecast_count:
            forecast_status = "PARTIAL"
        else:
            forecast_status = "NO_FORECAST"
        status = "OK" if counts["ranked"] else ("NEEDS_CHECK" if counts["pending"] else "NO_MATCH")
        return {
            "status": status,
            "request": {"region": region, "visit_at": visit.isoformat(), "outdoor": outdoor},
            "tag_version": "0.2", "catalog_version": catalog["version"], "catalog_source": catalog_source,
            "weather_mapping_version": mapping_version,
            "forecast_status": forecast_status, "counts": counts,
            **{bucket: [row for _, row in sorted(items)[:limit]] for bucket, items in decisions.items()},
        }

    return server


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="운영자가 지정한 공개 등록 데이터 JSON")
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS, help="매장별 프로젝트 날씨 지역 매핑 JSON")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--weather-base-url", help="기존 프로젝트 날씨 API의 기본 주소")
    modes.add_argument("--demo-open-meteo", action="store_true", help="비상업 검증용 공개 예보 사용")
    options = parser.parse_args()
    base_url = options.weather_base_url or (None if options.demo_open_meteo else os.environ.get("VENUE_WEATHER_BASE_URL"))
    create_server(
        options.catalog, options.locations, weather_base_url=base_url, demo_open_meteo=options.demo_open_meteo,
    ).run(transport="stdio")


if __name__ == "__main__":
    main()
