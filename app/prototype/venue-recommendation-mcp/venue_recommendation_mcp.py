"""손님용 v0.2 매장 추천 MCP. 기존 점주 문맥과 별도 stdio 서버로 실행합니다."""

import argparse
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from venue_csv_catalog import add_csv_venues
from venue_forecast import KST, fetch_forecasts, project_endpoint, timestamp
from venue_nearby import RADIUS_M, SeoulPlaceIndex, station_label
from venue_search_intent import BudgetBand, VenueCategory, budget_from_text, query_tags
from verify_tag_weather import expand, match_venues, normalize, read_tags, resolve, verify_pilot


ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "실매장_태그_날씨_시범_v0.2.json"
DEFAULT_LOCATIONS = ROOT / "venue_weather_regions_v0.2.json"
TAG_FILE = ROOT / "공통_태그_목록_v0.2.csv"
TagValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
WEATHER_PREFERENCES = {
    "CLEAR": (
        "맑은 날",
        {"mood_daylight", "mood_garden", "mood_urban_view", "mood_relaxed", "mood_lively"},
        {"menu_ade", "menu_cold_brew", "menu_iced_tea", "menu_bingsu", "menu_draft_beer"},
    ),
    "RAIN": (
        "비 오는 날",
        {"mood_cozy", "mood_calm", "mood_dim_lighting", "mood_gallery"},
        {"menu_soup", "menu_traditional_tea", "menu_hand_drip", "menu_filter_coffee", "menu_pajeon"},
    ),
}


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


class UnsupportedRegion(ValueError):
    """No venue in the available catalog covers the requested region."""


def region_venues(region, venues, locations):
    region_key = normalize(region)
    if region_key in {"서울", "서울시", "서울특별시"}:
        return venues

    selected = []
    for venue in venues:
        location = locations.get(venue["venue_id"], {})
        district = venue["district"]
        dong = venue.get("admin_dong") or location.get("admin_dong")
        address = {district}
        if dong:
            address.update({dong, f"{district} {dong}"})
        valid_names = {normalize(prefix + name) for prefix in ("", "서울 ", "서울특별시 ") for name in address}
        if region_key in valid_names:
            selected.append(venue)
    if not selected:
        raise UnsupportedRegion("지원 지역을 확인해 주세요. 예를 들어 강남구, 논현2동, 성수1가2동입니다.")
    return selected


def listed_budget(venue):
    price = venue.get("budget_example", {}).get("amount_krw", venue.get("avg_budget_krw"))
    return price if type(price) is int and price > 0 else None


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
                  venues_csv=None, weather_base_url=None, demo_open_meteo=False):
    tags, labels, ancestors = read_tags(TAG_FILE)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    verify_pilot(catalog, tags)
    mapping_version, locations = registered_locations(locations_path, catalog["venues"])
    if venues_csv is not None:
        catalog = add_csv_venues(catalog, locations, venues_csv, labels["MOOD"])
    project_url = project_endpoint(weather_base_url) if weather_base_url else None
    catalog_source = "PUBLIC_SOURCE_PILOT" if catalog_path.resolve() == DEFAULT_CATALOG else "CONFIGURED_PUBLIC_CATALOG"
    venues = {row["venue_id"]: row for row in catalog["venues"]}
    menus = {row["menu_id"]: row for row in catalog["menus"]}
    menu_ids_by_venue = {}
    expanded_menu_types = {}
    for menu_id, menu in menus.items():
        menu_ids_by_venue.setdefault(menu["venue_id"], []).append(menu_id)
        expanded_menu_types[menu_id] = expand(menu["menu_type_tag_ids"], ancestors)
    place_index = SeoulPlaceIndex()
    seoul_districts = set(place_index.districts.values())
    server = FastMCP("venue-recommendation")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @server.tool(annotations=read_only)
    def list_venue_tags(kind: Literal["MOOD", "CUISINE", "MENU_TYPE"] | None = None) -> dict[str, Any]:
        """점주와 손님이 함께 쓰는 분위기·요리 계열·메뉴 유형 및 시범 지역을 조회합니다."""
        available_regions = {"서울", *seoul_districts}
        available_regions.update(station_label(rows[0]["name"]) for rows in place_index.stations.values())
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
        region: Annotated[TagValue | None, Field(
            description="검색어에 장소가 없을 때 적용할 서울 자치구·행정동·역·주요 장소 이름. 생략 시 서울 전체.",
        )] = None,
        visit_at: Annotated[TagValue | None, Field(
            description="UTC 오프셋이 포함된 ISO 8601 방문 시각. 생략하면 요청 시각(KST).",
        )] = None,
        preferred_moods: Annotated[list[TagValue], Field(max_length=12)] | None = None,
        orders: Annotated[list[MenuOrder], Field(max_length=8)] | None = None,
        limit: Annotated[StrictInt, Field(ge=1, le=20)] = 10,
        venue_category: VenueCategory | None = None,
        required_moods: Annotated[list[TagValue], Field(max_length=12)] | None = None,
        budget: BudgetBand | None = None,
        budget_mode: Annotated[Literal["PREFER", "REQUIRE"], Field(
            description="PREFER는 예산에 가까운 순서, REQUIRE는 확인된 금액이 범위에 드는 매장만 반환.",
        )] = "PREFER",
        query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)] | None = None,
    ) -> dict[str, Any]:
        """날씨와 업종·분위기·메뉴·예산 조건으로 서울 매장을 추천합니다.

        자연어 query만으로 호출할 수 있습니다. 예를 들어 '홍대입구역 근처 카페' 또는 '성수카페거리 술집'.
        검색어에 장소가 있으면 그 장소를 기준으로 합니다. 동명이면 구 또는 노선 후보를 반환합니다.
        """
        visit = timestamp(visit_at) if visit_at is not None else datetime.now(KST)
        required = {resolve(value, labels, "MOOD") for value in required_moods or []}
        preferred = {resolve(value, labels, "MOOD") for value in preferred_moods or []}
        menu_orders = [item.model_dump() for item in orders or []]
        query_place = place_index.resolve(query or "")
        place = query_place
        if region and not place.area:
            if place.reason is None:
                place = place_index.resolve(region)
            elif place.reason == "STATION_REQUIRED":
                context = place_index.resolve(region)
                if context.area and context.area.kind == "STATION":
                    place = place_index.resolve(f"{context.area.label} {query}")

        search_location = place.area
        empty_reason = place.reason
        strict_budget = budget_mode == "REQUIRE"
        distances = {}
        if query is not None:
            inferred_category, inferred_moods, inferred_menus = query_tags(query_place.remaining_text, labels["MOOD"])
            if venue_category and inferred_category and venue_category != inferred_category:
                raise ValueError("검색어와 선택한 업종이 다릅니다.")
            venue_category = venue_category or inferred_category
            required.update(inferred_moods)
            if orders is None:
                menu_orders = [{"cuisine": None, "types": [tag]} for tag in inferred_menus]
            if budget is None:
                budget, explicit_limit = budget_from_text(query)
                strict_budget = strict_budget or explicit_limit
        spatial_search = bool(search_location and search_location.points)
        if query is not None and not (venue_category or required or preferred or menu_orders or budget
                                      or query_place.area or spatial_search or empty_reason):
            raise ValueError("찾는 지역이나 원하는 업종·분위기를 입력해 주세요.")

        region = search_location.label if search_location else (region or "서울")
        if empty_reason:
            scoped_venues = []
        elif spatial_search:
            scoped_venues = []
            for venue in venues.values():
                if venue["district"] not in seoul_districts or not (coordinates := venue.get("coordinates")):
                    continue
                distance = search_location.distance(coordinates)
                if distance <= RADIUS_M:
                    distances[venue["venue_id"]] = distance
                    scoped_venues.append(venue)
            if not scoped_venues:
                empty_reason = "NO_NEARBY_VENUES"
        elif search_location:
            scoped_venues = [
                venue for venue in venues.values() if venue["district"] in seoul_districts
                and search_location.includes(
                    venue["district"], venue.get("admin_dong") or locations.get(venue["venue_id"], {}).get("admin_dong"),
                )
            ]
            if not scoped_venues:
                empty_reason = "NO_REGISTERED_VENUES"
        else:
            try:
                scoped_venues = region_venues(region, catalog["venues"], locations)
            except UnsupportedRegion:
                if query is None:
                    raise
                scoped_venues = []
                empty_reason = "REGION_UNSUPPORTED"

        if scoped_venues and (venue_category or spatial_search):
            allowed_categories = {venue_category} if venue_category else {"CAFE", "PUB"}
            scoped_venues = [row for row in scoped_venues if row.get("venue_category") in allowed_categories]
            if not scoped_venues:
                empty_reason = "NO_CATEGORY"
        if scoped_venues and required:
            scoped_venues = [row for row in scoped_venues if required <= expand(row["mood_tag_ids"], ancestors)]
            if not scoped_venues:
                empty_reason = "NO_MOOD"
        if strict_budget and budget is None:
            raise ValueError("예산 범위를 입력해 주세요.")
        if scoped_venues and strict_budget:
            scoped_venues = [
                row for row in scoped_venues if (price := listed_budget(row)) is not None
                and budget.distance(price) == 0
            ]
            if not scoped_venues:
                empty_reason = "NO_BUDGET"
        conditions = {
            "district": None, "preferred_moods": sorted(preferred | required), "orders": menu_orders,
        }
        base = list(match_venues({**catalog, "venues": scoped_venues}, conditions, labels, ancestors))
        if scoped_venues and not base:
            empty_reason = "NO_MENU"
        by_venue = await fetch_forecasts(
            {venue["venue_id"]: locations.get(venue["venue_id"]) for venue, _ in base}, visit,
            project_url=project_url, demo_open_meteo=demo_open_meteo,
        )

        ranked = []
        for venue, matched in base:
            venue_id = venue["venue_id"]
            forecast = by_venue[venue_id]
            location = locations.get(venue_id, {})
            distance = distances.get(venue_id)
            budget_example = venue.get("budget_example")
            price = listed_budget(venue)
            budget_distance = budget.distance(price) if budget and price is not None else None

            weather_score = 0
            weather_match = None
            if forecast.state != "NO_FORECAST" and (weather := WEATHER_PREFERENCES.get(forecast.condition)):
                weather_label, weather_moods, weather_menu_types = weather
                mood_matches = sorted(weather_moods & set(venue["mood_tag_ids"]))
                # A requested menu uses the same verified items that satisfied the search.
                requested_items = {menu_id for group in matched["matched_menu_groups"] for menu_id in group}
                menu_matches = sorted(
                    menus[menu_id]["name"] for menu_id in (requested_items or menu_ids_by_venue.get(venue_id, ()))
                    if expanded_menu_types[menu_id] & weather_menu_types
                )
                reasons = [tags[tag_id]["label_ko"] for tag_id in mood_matches[:1]] + menu_matches[:1]
                weather_score = len(reasons)
                if reasons:
                    weather_match = {"label": " · ".join([weather_label, *reasons]), "source": "TAG_MATCH"}
            result = {
                "venue_id": venue_id, "name": venue["name"], "address": venue["address"],
                "district": venue["district"], "admin_dong": venue.get("admin_dong") or location.get("admin_dong"),
                "venue_category": venue.get("venue_category"), "sub_category": venue.get("sub_category"),
                "avg_budget_krw": None if budget_example else price,
                **({"budget_example": budget_example} if budget_example else {}),
                **({"representative_menus": venue["public_listing"]["representative_menus"]} if "public_listing" in venue else {}),
                **({"distance_m": round(distance)} if distance is not None else {}),
                **({"weather_match": weather_match} if weather_match else {}),
                "matched_moods": [{"id": tag_id, "label": tags[tag_id]["label_ko"]}
                                  for tag_id in matched["matched_mood_ids"]],
                "matched_orders": [
                    [{"menu_id": menu_id, "name": menus[menu_id]["name"],
                      "cuisine_tag_ids": menus[menu_id]["cuisine_tag_ids"],
                      "menu_type_tag_ids": menus[menu_id]["menu_type_tag_ids"]}
                     for menu_id in group]
                    for group in matched["matched_menu_groups"]
                ],
                "forecast": {**asdict(forecast), "region_id": location.get("region_id"),
                             "grid": [location["nx"], location["ny"]] if location else None},
            }
            # Missing budgets follow known budgets within the same relevance tier.
            tie_breakers = (
                budget is not None and budget_distance is None, budget_distance or 0,
                distance if distance is not None else 0, venue_id,
            )
            mood_priority = -len(result["matched_moods"])
            ranked.append(((-weather_score, mood_priority, *tie_breakers), result))

        counts = {"ranked": len(ranked), "pending": 0, "excluded": 0}
        forecast_count = sum(value.state != "NO_FORECAST" for value in by_venue.values())
        if not by_venue:
            forecast_status = "NOT_NEEDED"
        elif forecast_count == len(by_venue):
            forecast_status = "AVAILABLE"
        elif forecast_count:
            forecast_status = "PARTIAL"
        else:
            forecast_status = "NO_FORECAST"
        status = "OK" if ranked else ("NEEDS_CHECK" if place.choices else "NO_MATCH")
        return {
            "status": status,
            **({"empty_reason": empty_reason} if empty_reason else {}),
            **({"location_candidates": place.choices} if place.choices else {}),
            "request": {
                "region": region, "visit_at": visit.isoformat(),
                "venue_category": venue_category, "required_moods": sorted(required),
                "budget": budget.model_dump() if budget else None,
                **({"budget_mode": "REQUIRE" if strict_budget else "PREFER"} if budget else {}),
                **({"location": search_location.public()} if search_location else {}),
            },
            "tag_version": "0.2", "catalog_version": catalog["version"], "catalog_source": catalog_source,
            "weather_mapping_version": mapping_version,
            "forecast_status": forecast_status, "counts": counts,
            "ranked": [row for _, row in sorted(ranked)[:limit]], "pending": [], "excluded": [],
        }

    return server


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="운영자가 지정한 공개 등록 데이터 JSON")
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS, help="매장별 프로젝트 날씨 지역 매핑 JSON")
    parser.add_argument("--venues-csv", type=Path, help="원본 ID로 매장 후보와 1인 평균예산을 추가할 시연 CSV")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--weather-base-url", help="기존 프로젝트 날씨 API의 기본 주소")
    modes.add_argument("--demo-open-meteo", action="store_true", help="비상업 검증용 공개 예보 사용")
    options = parser.parse_args()
    base_url = options.weather_base_url or (None if options.demo_open_meteo else os.environ.get("VENUE_WEATHER_BASE_URL"))
    create_server(
        options.catalog, options.locations, venues_csv=options.venues_csv,
        weather_base_url=base_url, demo_open_meteo=options.demo_open_meteo,
    ).run(transport="stdio")


if __name__ == "__main__":
    main()
