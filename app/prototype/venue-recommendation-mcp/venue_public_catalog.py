"""Read a published snapshot of the existing venue/menu catalog by stable ID."""

import os
from threading import Lock
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StringConstraints, ValidationError

from venue_csv_catalog import is_out_of_scope_facility
from venue_nearby import PROVINCE_LABELS
from venue_search_intent import VenueCategory
from venue_visit_purposes import RESEARCHED_MOODS, reviewed_venue_tags


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Basis = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class CatalogSnapshot(BaseModel):
    model_config = ConfigDict(strict=True)

    version: Text
    venues: list[dict[str, Any]]
    menus: list[dict[str, Any]]
    preview_only: bool = False
    independently_verified_menus: Annotated[int, Field(ge=0)] | None = None
    source_basis: Basis | None = None
    visibility_basis: Basis | None = None
    budget_basis: Basis | None = None
    tag_basis: Basis | None = None


class VenueResearch(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    description: str = ""
    seating: str = ""
    sources: list[Text] = Field(default_factory=list)
    limitations: str = ""
    checked_on: str = ""


class PublicVenue(BaseModel):
    model_config = ConfigDict(strict=True)

    venue_id: Text
    name: Text
    address: Text
    region_id: Text
    venue_category: VenueCategory
    sub_category: Text | None = None
    coordinates: Annotated[list[FiniteFloat], Field(min_length=2, max_length=2)] | None = None
    mood_tag_ids: list[Text] = Field(default_factory=list)
    avg_budget_krw: Annotated[int, Field(gt=0)] | None = None
    research: VenueResearch | None = None


class PublicMenu(BaseModel):
    model_config = ConfigDict(strict=True)

    menu_id: Text
    venue_id: Text
    name: Text
    cuisine_tag_ids: list[Text] = Field(default_factory=list)
    menu_type_tag_ids: list[Text] = Field(default_factory=list)
    price_krw: Annotated[int, Field(ge=0)] | None = None


def require_unique_ids(rows, key):
    ids = [value.strip() for row in rows if isinstance(value := row.get(key), str) and value.strip()]
    if len(ids) != len(set(ids)):
        raise ValueError(f"공개 스냅샷의 {key}가 중복됩니다.")


def require_tags(ids, kind, tags):
    if any(tags.get(tag_id, {}).get("kind") != kind for tag_id in ids):
        raise ValueError(f"공개 스냅샷에 등록되지 않은 {kind} 태그가 있습니다.")


def snapshot_identity(stream):
    info = os.fstat(stream.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class PublicCatalogReader:
    """Reuse one validated snapshot until the publisher atomically replaces its file."""

    def __init__(self, path, weather_regions, tags, *, allow_preview=False):
        self.path = path
        self.weather_regions = weather_regions
        self.tags = tags
        self.allow_preview = allow_preview
        self._cached = None
        self._lock = Lock()

    def read(self):
        with self._lock:
            try:
                with self.path.open("rb") as stream:
                    identity = snapshot_identity(stream)
                    if self._cached is not None and self._cached[0] == identity:
                        return self._cached[1]
                    content = stream.read()
                    if snapshot_identity(stream) != identity:
                        raise ValueError("공개 매장·메뉴 스냅샷을 읽는 중에 파일이 변경되었습니다.")
            except OSError:
                raise ValueError("공개 매장·메뉴 스냅샷을 읽을 수 없습니다.") from None

            result = parse_public_catalog(content, self.weather_regions, self.tags, allow_preview=self.allow_preview)
            self._cached = identity, result
            return result


def read_public_catalog(path, weather_regions, tags, *, allow_preview=False):
    return PublicCatalogReader(path, weather_regions, tags, allow_preview=allow_preview).read()


def parse_public_catalog(content, weather_regions, tags, *, allow_preview):
    try:
        snapshot = CatalogSnapshot.model_validate_json(content)
    except ValidationError:
        raise ValueError("공개 매장·메뉴 스냅샷을 읽을 수 없습니다.") from None
    if not allow_preview:
        if 'preview_only' not in snapshot.model_fields_set:
            raise ValueError("운영 스냅샷은 preview_only=false를 명시해야 합니다.")
        if snapshot.preview_only:
            raise ValueError("로컬 시연용 스냅샷은 --allow-preview-catalog를 명시해야 읽을 수 있습니다.")

    require_unique_ids(snapshot.venues, "venue_id")
    require_unique_ids(snapshot.menus, "menu_id")
    venues = {}
    menus = []
    locations = {}
    try:
        for row in snapshot.venues:
            if row.get("is_public") is not True:
                continue
            venue = PublicVenue.model_validate(row)
            if is_out_of_scope_facility(venue.name):
                continue
            region = weather_regions.get(venue.region_id)
            if region is None:
                raise ValueError("공개 매장의 지역 ID가 프로젝트 날씨 매핑에 없습니다.")
            if region["sido"] not in PROVINCE_LABELS or not region["sigungu"]:
                continue
            require_tags(venue.mood_tag_ids, "MOOD", tags)
            if venue.coordinates:
                lat, lng = venue.coordinates
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    raise ValueError("공개 매장의 좌표가 위도·경도 범위를 벗어났습니다.")
            location = {
                "region_id": venue.region_id, "province": region["sido"], "district": region["sigungu"],
                "admin_dong": region["name"], "nx": region["nx"], "ny": region["ny"],
            }
            if venue.coordinates:
                location.update(latitude=lat, longitude=lng)
            locations[venue.venue_id] = location
            visit_purposes, reviewed_moods = reviewed_venue_tags(venue.venue_category, venue.research)
            mood_ids = venue.mood_tag_ids
            if snapshot.preview_only:
                mood_ids = sorted(
                    (set(mood_ids) - RESEARCHED_MOODS.keys())
                    | (reviewed_moods & tags.keys())
                )
            venues[venue.venue_id] = {
                **venue.model_dump(exclude={"region_id", "mood_tag_ids"}, exclude_none=True),
                "mood_tag_ids": mood_ids,
                **({"visit_purposes": sorted(visit_purposes)} if visit_purposes else {}),
                "province": location["province"], "district": location["district"], "admin_dong": location["admin_dong"],
            }

        for row in snapshot.menus:
            parent = row.get("venue_id")
            if row.get("is_public") is not True or not isinstance(parent, str) or parent.strip() not in venues:
                continue
            menu = PublicMenu.model_validate(row)
            require_tags(menu.cuisine_tag_ids, "CUISINE", tags)
            require_tags(menu.menu_type_tag_ids, "MENU_TYPE", tags)
            menus.append(menu.model_dump(exclude_none=True))
    except ValidationError:
        raise ValueError("공개 매장·메뉴 스냅샷의 필드 형식을 확인해 주세요.") from None

    metadata = snapshot.model_dump(exclude={"version", "venues", "menus"}, exclude_none=True)
    return {"version": snapshot.version, "venues": list(venues.values()), "menus": menus, "metadata": metadata}, locations
