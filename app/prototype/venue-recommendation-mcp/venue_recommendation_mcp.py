"""손님용 v0.2 매장 추천 MCP. 기존 점주 문맥과 별도 stdio 서버로 실행합니다."""

import argparse
import asyncio
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
from venue_nearby import PROVINCE_LABELS, RADIUS_M, SeoulPlaceIndex, display_district, station_label
from venue_public_catalog import PublicCatalogReader
from venue_search_intent import (
    APPROXIMATE_BUDGET, BudgetBand, CALM_WORDS, UnverifiedCondition, VenueCategory, budget_from_text,
    compile_menu_words, explicit_menu_tags, per_person_budget, query_tags, required_mood_tags,
    unverified_venue_conditions, venue_categories,
)
from verify_tag_weather import expand, match_venues, normalize, read_tags, resolve, verify_pilot


ROOT = Path(__file__).resolve().parent
DEFAULT_CATALOG = ROOT / "실매장_태그_날씨_시범_v0.2.json"
DEFAULT_LOCATIONS = ROOT / "venue_weather_regions_v0.2.json"
TAG_FILE = ROOT / "공통_태그_목록_v0.2.csv"
TagValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
RequestText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
MENU_PAIRINGS = {
    "menu_beer": ("menu_chicken", "menu_skewer", "menu_fries", "menu_soup"),
    "menu_makgeolli": ("menu_jeon",),
    "menu_soju": ("menu_soup",),
    "menu_wine": ("menu_cheese_plate", "menu_charcuterie"),
    "menu_highball": ("menu_skewer", "menu_fried"),
    "menu_coffee": ("menu_dessert", "menu_bread"),
    "menu_tea": ("menu_rice_cake", "menu_dessert"),
}
WEATHER_PREFERENCES = {
    "CLEAR": (
        "맑은 날",
        {"mood_daylight", "mood_garden", "mood_urban_view", "mood_relaxed", "mood_lively"},
        {"menu_ade", "menu_cold_brew", "menu_iced_tea", "menu_bingsu", "menu_draft_beer"},
    ),
    "RAIN": (
        "비 오는 날",
        {"mood_cozy", "mood_calm", "mood_dim_lighting", "mood_gallery"},
        {"menu_soup", "menu_traditional_tea", "menu_hand_drip", "menu_filter_coffee", "menu_jeon"},
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


def preserve_menu_orders(orders, requested, only_menu, labels, ancestors):
    """Recover explicit orders without adding optional pairings as requirements."""
    if not requested:
        return orders
    required = set(requested)
    covered = set()
    effective = []
    for order in orders:
        types = [resolve(value, labels, "MENU_TYPE") for value in order["types"]]
        if only_menu:
            types = [tag for tag in types if required & ancestors[tag]]
            if not types:
                continue
        matches = required & expand(types, ancestors)
        covered.update(matches)
        if not order["cuisine"] and len(types) > 1 and len(matches) == len(types):
            # A model can put two explicitly separate items into one menu's tag list.
            effective.extend({"cuisine": None, "types": [tag]} for tag in types)
        else:
            effective.append({**order, "types": types})
    effective.extend({"cuisine": None, "types": [tag]} for tag in requested if tag not in covered)
    return effective


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
                  venues_csv=None, public_catalog_path=None, allow_preview_catalog=False,
                  weather_base_url=None, demo_open_meteo=False):
    tags, labels, ancestors = read_tags(TAG_FILE)
    labels["MOOD"].update({word: "mood_calm" for word in CALM_WORDS})
    menu_words, menu_pattern = compile_menu_words(labels["MENU_TYPE"])
    public_mode = public_catalog_path is not None
    if public_mode and (venues_csv is not None or catalog_path != DEFAULT_CATALOG or locations_path != DEFAULT_LOCATIONS):
        raise ValueError("공개 스냅샷과 시연 카탈로그·CSV·지역 매핑은 함께 설정할 수 없습니다.")
    place_index = SeoulPlaceIndex(capital=public_mode)
    default_region = "수도권" if public_mode else "서울"
    if public_mode:
        mapping_version = place_index.weather_mapping_version
        catalog_source = "PUBLIC_SNAPSHOT"
        public_catalog = PublicCatalogReader(
            public_catalog_path, place_index.weather_regions, tags, allow_preview=allow_preview_catalog,
        )
    else:
        demo_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        verify_pilot(demo_catalog, tags)
        mapping_version, demo_locations = registered_locations(locations_path, demo_catalog["venues"])
        if venues_csv is not None:
            demo_catalog = add_csv_venues(demo_catalog, demo_locations, venues_csv, labels["MOOD"])
        catalog_source = (
            "PUBLIC_SOURCE_PILOT" if catalog_path.resolve() == DEFAULT_CATALOG else "CONFIGURED_PUBLIC_CATALOG"
        )

    def current_catalog():
        if public_mode:
            return public_catalog.read()
        return demo_catalog, demo_locations

    project_url = project_endpoint(weather_base_url) if weather_base_url else None
    caller_text = os.environ.get("VENUE_MCP_REQUEST_TEXT", "")[:240]
    seoul_districts = set(place_index.districts.values())
    server = FastMCP("venue-recommendation")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)

    @server.tool(annotations=read_only)
    def list_venue_tags(kind: Literal["MOOD", "CUISINE", "MENU_TYPE"] | None = None) -> dict[str, Any]:
        """점주와 손님이 함께 쓰는 분위기·요리 계열·메뉴 유형 및 검색 지역을 조회합니다."""
        catalog, locations = current_catalog()
        available_regions = set(place_index.region_labels)
        available_regions.update(station_label(rows[0]["name"]) for rows in place_index.stations.values())
        for venue in catalog["venues"]:
            dong = venue.get("admin_dong") or locations.get(venue["venue_id"], {}).get("admin_dong")
            if dong:
                if public_mode:
                    province = PROVINCE_LABELS[venue["province"]]
                    available_regions.add(f"{province} {display_district(venue['district'])} {dong}")
                else:
                    available_regions.add(dong)
        return {
            "tag_version": "0.2",
            **({"catalog_metadata": catalog["metadata"]} if "metadata" in catalog else {}),
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
            description="검색어에 장소가 없을 때 적용할 시·도·시군구·행정동·역·주요 장소 이름. 생략 시 설정 지역 전체.",
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
        query: RequestText | None = None,
        unverified_conditions: Annotated[list[UnverifiedCondition], Field(
            max_length=8, description="매장 데이터에서 확인할 수 없는 조건을 원문대로 전달. 예: 헌팅, 소개팅, 휠체어 접근.",
        )] | None = None,
        request_text: Annotated[RequestText | None, Field(
            description="호스트가 전달하는 원문. 명시한 지역·업종·메뉴·1인 예산과 미확인 조건을 보존합니다.",
        )] = None,
    ) -> dict[str, Any]:
        """날씨와 업종·분위기·메뉴·예산 조건으로 매장을 추천합니다.

        자연어 query만으로 호출할 수 있습니다. 예를 들어 '홍대입구역 근처 카페' 또는 '성수카페거리 술집'.
        검색어에 장소가 있으면 그 장소를 기준으로 합니다. 동명이면 구 또는 노선 후보를 반환합니다.
        주문에 맞는 매장에 확인된 곁들임이 있으면 paired_menu로 제안합니다.
        """
        visit = timestamp(visit_at) if visit_at is not None else datetime.now(KST)
        host_text = caller_text or request_text or ""
        original = host_text or query or ""
        original_place = place_index.resolve(original)
        place = original_place
        if region and not place.area:
            if place.reason is None:
                place = place_index.resolve(region)
            elif place.reason == "STATION_REQUIRED":
                context = place_index.resolve(region)
                if context.area and context.area.kind == "STATION":
                    place = place_index.resolve(f"{context.area.label} {original}")

        search_location = place.area
        location_label = search_location.label if search_location else (None if place.reason else region or default_region)

        def needs_check(conditions):
            return {
                "status": "NEEDS_CHECK", "empty_reason": "CONDITION_UNVERIFIED", "unverified_conditions": conditions,
                "request": {
                    "region": location_label,
                    "visit_at": visit.isoformat(), "venue_category": venue_category,
                    **({"location": search_location.public()} if search_location else {}),
                },
                "tag_version": "0.2", "catalog_version": None, "catalog_source": catalog_source,
                "weather_mapping_version": mapping_version, "forecast_status": "NOT_NEEDED",
                "counts": {"ranked": 0, "pending": 0, "excluded": 0}, "ranked": [], "pending": [], "excluded": [],
            }

        unknown = unverified_venue_conditions(original, unverified_conditions, menu_words, menu_pattern)
        if unknown:
            return needs_check(unknown)

        required = {resolve(value, labels, "MOOD") for value in required_moods or []}
        preferred = {resolve(value, labels, "MOOD") for value in preferred_moods or []}
        menu_orders = [item.model_dump() for item in orders or []]
        empty_reason = place.reason
        strict_budget = budget_mode == "REQUIRE"
        distances = {}
        allow_pairing = query is None
        remaining = original_place.remaining_text
        phrase = normalize(remaining)
        if query is not None:
            inferred_category, inferred_moods, inferred_menus, allow_pairing = query_tags(
                remaining, labels["MOOD"], menu_words, menu_pattern, ancestors,
            )
            if venue_category and inferred_category and venue_category != inferred_category:
                raise ValueError("검색어와 선택한 업종이 다릅니다.")
            venue_category = venue_category or inferred_category
            required.update(inferred_moods)
            if orders is None:
                menu_orders = [{"cuisine": None, "types": [tag]} for tag in inferred_menus]
        else:
            categories = venue_categories(phrase, menu_words, menu_pattern, explicit_target=True)
            if len(categories) == 1:
                venue_category = next(iter(categories))
            required.update(required_mood_tags(phrase, labels["MOOD"], honor_preference=True))
        if host_text or (orders is not None and query is not None):
            requested, only_menu = explicit_menu_tags(phrase, menu_words, menu_pattern)
            menu_orders = preserve_menu_orders(menu_orders, requested, only_menu, labels, ancestors)
            allow_pairing = allow_pairing and not only_menu
        try:
            original_budget, explicit_limit = (
                budget_from_text(query) if query is not None else per_person_budget(host_text)
            )
        except ValueError:
            if query is not None:
                raise
            return needs_check([{"kind": "OTHER", "text": "1인 예산 범위", "evidence_status": "UNKNOWN"}])
        if original_budget is not None:
            if explicit_limit or APPROXIMATE_BUDGET.search(original):
                budget, strict_budget = original_budget, explicit_limit
            elif budget is None:
                budget = original_budget
        preferred.difference_update(required)
        spatial_search = bool(search_location and search_location.points)
        if query is not None and not (venue_category or required or preferred or menu_orders or budget
                                      or original_place.area or spatial_search or empty_reason):
            raise ValueError("찾는 지역이나 원하는 업종·분위기를 입력해 주세요.")
        if strict_budget and budget is None:
            raise ValueError("예산 범위를 입력해 주세요.")

        catalog, locations = await asyncio.to_thread(current_catalog) if public_mode else current_catalog()
        region = search_location.label if search_location else (region or default_region)
        if empty_reason:
            scoped_venues = []
        elif search_location:
            scoped_venues = [
                venue for venue in catalog["venues"] if (public_mode or venue["district"] in seoul_districts)
                and search_location.includes(
                    venue["district"], venue.get("admin_dong") or locations.get(venue["venue_id"], {}).get("admin_dong"),
                    venue.get("province"),
                )
            ]
            if spatial_search:
                for venue in scoped_venues:
                    if coordinates := venue.get("coordinates"):
                        distance = search_location.distance(coordinates)
                        if distance <= RADIUS_M:
                            distances[venue["venue_id"]] = distance
                scoped_venues = [venue for venue in scoped_venues if venue["venue_id"] in distances]
            if not scoped_venues:
                empty_reason = "NO_NEARBY_VENUES" if spatial_search else "NO_REGISTERED_VENUES"
        elif public_mode:
            scoped_venues = catalog["venues"] if region == default_region else []
            if not scoped_venues:
                empty_reason = "NO_REGISTERED_VENUES" if region == default_region else "REGION_UNSUPPORTED"
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
        if scoped_venues and strict_budget:
            scoped_venues = [
                row for row in scoped_venues if (price := listed_budget(row)) is not None
                and budget.distance(price) == 0
            ]
            if not scoped_venues:
                empty_reason = "NO_BUDGET"
        pairing_types = ()
        if allow_pairing and len(menu_orders) == 1:
            order = menu_orders[0]
            if not order["cuisine"] and len(order["types"]) == 1:
                chosen_type = resolve(order["types"][0], labels, "MENU_TYPE")
                pairing_types = next(
                    (sides for main, sides in MENU_PAIRINGS.items() if main in ancestors[chosen_type]),
                    (),
                )

        conditions = {
            "district": None, "preferred_moods": sorted(preferred | required), "orders": menu_orders,
        }
        scoped_ids = {venue["venue_id"] for venue in scoped_venues}
        scoped_menus = [menu for menu in catalog["menus"] if menu["venue_id"] in scoped_ids]
        base = list(match_venues({"venues": scoped_venues, "menus": scoped_menus}, conditions, labels, ancestors))
        if scoped_venues and not base:
            empty_reason = "NO_MENU"
        menus = {menu["menu_id"]: menu for menu in scoped_menus}
        menu_ids_by_venue = {}
        expanded_menu_types = {}
        for menu_id, menu in menus.items():
            menu_ids_by_venue.setdefault(menu["venue_id"], []).append(menu_id)
            expanded_menu_types[menu_id] = expand(menu["menu_type_tag_ids"], ancestors)
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

            requested_items = {menu_id for group in matched["matched_menu_groups"] for menu_id in group}
            paired_menu = None
            pairing_priority = len(pairing_types)
            pair_candidates = menu_ids_by_venue.get(venue_id, ())
            if pairing_types and strict_budget:
                # 예산 상한이 있으면 확인된 주문 예시 안에서만 조합을 제안합니다.
                example_items = {item["name"] for item in (budget_example or {}).get("items", ())}
                if any(menus[menu_id]["name"] in example_items for menu_id in requested_items):
                    pair_candidates = [menu_id for menu_id in pair_candidates if menus[menu_id]["name"] in example_items]
                else:
                    pair_candidates = ()
            for priority, side in enumerate(pairing_types):
                paired_menu = next((
                    menus[menu_id] for menu_id in pair_candidates
                    if menu_id not in requested_items and side in expanded_menu_types[menu_id]
                ), None)
                if paired_menu:
                    pairing_priority = priority
                    break

            weather_score = 0
            weather_match = None
            if forecast.state != "NO_FORECAST" and (weather := WEATHER_PREFERENCES.get(forecast.condition)):
                weather_label, weather_moods, weather_menu_types = weather
                mood_matches = sorted(weather_moods & set(venue["mood_tag_ids"]))
                weather_items = requested_items or menu_ids_by_venue.get(venue_id, ())
                if paired_menu:
                    weather_items = requested_items | {paired_menu["menu_id"]}
                menu_matches = sorted(
                    menus[menu_id]["name"] for menu_id in weather_items
                    if expanded_menu_types[menu_id] & weather_menu_types
                )
                reasons = [tags[tag_id]["label_ko"] for tag_id in mood_matches[:1]] + menu_matches[:1]
                weather_score = len(reasons)
                if reasons:
                    weather_match = {"label": " · ".join([weather_label, *reasons]), "source": "TAG_MATCH"}
            result = {
                "venue_id": venue_id, "name": venue["name"], "address": venue["address"],
                "district": venue["district"], "admin_dong": venue.get("admin_dong") or location.get("admin_dong"),
                **({"province": venue["province"]} if public_mode else {}),
                "venue_category": venue.get("venue_category"), "sub_category": venue.get("sub_category"),
                **({"research": venue["research"]} if venue.get("research") else {}),
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
                      "menu_type_tag_ids": menus[menu_id]["menu_type_tag_ids"],
                      **({"price_krw": menus[menu_id]["price_krw"]} if "price_krw" in menus[menu_id] else {})}
                     for menu_id in group]
                    for group in matched["matched_menu_groups"]
                ],
                "forecast": {**asdict(forecast), "region_id": location.get("region_id"),
                             "grid": [location["nx"], location["ny"]] if location else None},
            }
            if paired_menu:
                result["paired_menu"] = {
                    key: paired_menu[key] for key in ("menu_id", "name", "price_krw") if key in paired_menu
                }
            # Missing budgets follow known budgets within the same relevance tier.
            tie_breakers = (
                budget is not None and budget_distance is None, budget_distance or 0,
                distance if distance is not None else 0, venue_id,
            )
            mood_priority = -len(result["matched_moods"])
            rank = (paired_menu is None, pairing_priority != 0, -weather_score, mood_priority, *tie_breakers)
            ranked.append((rank, result))

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
                "preferred_moods": sorted(preferred),
                **({"orders": menu_orders} if menu_orders else {}),
                "budget": budget.model_dump() if budget else None,
                **({"budget_mode": "REQUIRE" if strict_budget else "PREFER"} if budget else {}),
                **({"location": search_location.public()} if search_location else {}),
            },
            "tag_version": "0.2", "catalog_version": catalog["version"], "catalog_source": catalog_source,
            **({"catalog_metadata": catalog["metadata"]} if "metadata" in catalog else {}),
            "weather_mapping_version": mapping_version,
            "forecast_status": forecast_status, "counts": counts,
            "ranked": [row for _, row in sorted(ranked)[:limit]], "pending": [], "excluded": [],
        }

    return server


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    catalogs = parser.add_mutually_exclusive_group()
    catalogs.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help="고정 시연 카탈로그 JSON")
    catalogs.add_argument("--public-catalog", type=Path, help="수도권 공개 매장·메뉴 스냅샷 JSON")
    parser.add_argument("--allow-preview-catalog", action="store_true", help="로컬 시연 스냅샷을 명시적으로 허용")
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS, help="매장별 프로젝트 날씨 지역 매핑 JSON")
    parser.add_argument("--venues-csv", type=Path, help="원본 ID로 매장 후보와 1인 평균예산을 추가할 시연 CSV")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--weather-base-url", help="기존 프로젝트 날씨 API의 기본 주소")
    modes.add_argument("--demo-open-meteo", action="store_true", help="비상업 검증용 공개 예보 사용")
    options = parser.parse_args()
    if options.public_catalog and (options.venues_csv or options.locations != DEFAULT_LOCATIONS):
        parser.error("--public-catalog는 --venues-csv 또는 --locations와 함께 사용할 수 없습니다.")
    if options.allow_preview_catalog and options.public_catalog is None:
        parser.error("--allow-preview-catalog에는 --public-catalog가 필요합니다.")
    base_url = options.weather_base_url or (None if options.demo_open_meteo else os.environ.get("VENUE_WEATHER_BASE_URL"))
    create_server(
        options.catalog, options.locations, venues_csv=options.venues_csv, public_catalog_path=options.public_catalog,
        allow_preview_catalog=options.allow_preview_catalog,
        weather_base_url=base_url, demo_open_meteo=options.demo_open_meteo,
    ).run(transport="stdio")


if __name__ == "__main__":
    main()
