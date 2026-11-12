"""Read the deployed team catalog without falling back to private owner rows."""

from collections import OrderedDict, defaultdict
from contextlib import closing
from functools import lru_cache
import gzip
from io import BytesIO, TextIOWrapper
import json
from math import isfinite
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
import unicodedata
import zlib

from .csv_lazy_repo import CsvLazyRepository


class CatalogUnavailable(Exception):
    """The configured published catalog could not be read consistently."""


PROVINCES = {
    "서울": "서울특별시", "서울시": "서울특별시", "서울특별시": "서울특별시",
    "인천": "인천광역시", "인천시": "인천광역시", "인천광역시": "인천광역시",
    "경기": "경기도", "경기도": "경기도",
}
FACILITIES = (
    "어린이집", "유치원", "초등학교", "중학교", "고등학교", "특수학교",
    "집단급식소", "구내식당", "보건소", "보건지소", "보건진료소", "경로당",
    "요양원", "요양병원", "노인복지관", "주간보호센터",
)


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def source_region(row):
    address = (row.get("도로명주소") or "").strip()
    province = PROVINCES.get(address.split()[0]) if address else None
    district = normalized(row.get("자치구") or "")
    for prefix in sorted(PROVINCES, key=len, reverse=True):
        if district.startswith(prefix):
            district = district[len(prefix):]
            break
    return province, district


def requested_region(value):
    query = normalized(value)
    province = None
    for prefix in sorted(PROVINCES, key=len, reverse=True):
        if query.startswith(prefix):
            province = PROVINCES[prefix]
            query = query[len(prefix):]
            break
    if province is None and query in {"중구", "서구"}:
        province = "서울특별시"
    return province, query


def is_listable(row):
    name = (row.get("매장명") or "").strip()
    address = (row.get("도로명주소") or "").strip()
    name_key = re.sub(r"\([^()]*\)$", "", normalized(name))
    return bool(
        row.get("venue_id") and name and address and source_region(row)[0]
        and (row.get("업종구분") or "").strip() in {"카페", "술집", "주점"}
        and (row.get("영업상태_코드") or "").strip() in {"OPEN", "1"}
        and not name_key.endswith(FACILITIES)
    )


def coordinate(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def catalog_identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _CatalogIndex:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = RLock()
        self.identity = None
        self.buckets = {}
        self.region_cache = OrderedDict()

    def current_identity(self):
        try:
            return catalog_identity(self.path)
        except OSError as error:
            raise CatalogUnavailable("매장 카탈로그를 읽을 수 없습니다.") from error

    def connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def snapshot(self):
        with self.lock:
            identity = self.current_identity()
            if identity != self.identity:
                self._load(identity)
            return PublishedCatalogRepository(self, identity)

    def verify(self, identity):
        if self.current_identity() != identity:
            raise CatalogUnavailable("매장 카탈로그가 교체되어 요청을 다시 실행해야 합니다.")

    def _load(self, identity):
        try:
            with closing(self.connect()) as db:
                row = db.execute("SELECT value FROM metadata WHERE key = 'catalog'").fetchone()
                metadata = json.loads(row[0]) if row else {}
                if metadata.get("schema") != 1 or metadata.get("preview_only") is not False:
                    raise ValueError("운영 카탈로그 형식이 아닙니다.")
                db.execute("SELECT venue_id, raw FROM venues LIMIT 0")
                db.execute("SELECT menu_id, venue_id, raw FROM menus LIMIT 0")
                row = db.execute("SELECT value FROM metadata WHERE key = 'venue_rows_gzip'").fetchone()
                if not row or not isinstance(row[0], bytes):
                    raise ValueError("매장 색인이 없습니다.")
                compressors, parts, seen = {}, defaultdict(list), set()
                with TextIOWrapper(gzip.GzipFile(fileobj=BytesIO(row[0])), encoding="utf-8") as source:
                    for line in source:
                        raw = json.loads(line)
                        if not isinstance(raw, dict):
                            raise ValueError("매장 데이터 형식이 올바르지 않습니다.")
                        venue_id = (raw.get("venue_id") or "").strip()
                        if not venue_id or venue_id in seen:
                            raise ValueError("빈 매장 ID 또는 중복 매장 ID가 있습니다.")
                        seen.add(venue_id)
                        if not is_listable(raw):
                            continue
                        key = source_region(raw)
                        if key not in compressors:
                            compressors[key] = zlib.compressobj(level=1)
                        parts[key].append(compressors[key].compress(line.encode("utf-8")))
                buckets = {
                    key: b"".join(parts[key] + [compressor.flush()])
                    for key, compressor in compressors.items()
                }
            self.verify(identity)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, EOFError, zlib.error) as error:
            raise CatalogUnavailable("매장 카탈로그를 읽을 수 없습니다.") from error
        self.buckets = buckets
        self.region_cache.clear()
        self.identity = identity

    def region_rows(self, identity, key):
        with self.lock:
            self.verify(identity)
            if self.identity != identity:
                raise CatalogUnavailable("매장 카탈로그가 교체되어 요청을 다시 실행해야 합니다.")
            if key in self.region_cache:
                self.region_cache.move_to_end(key)
                return self.region_cache[key]
            try:
                text = zlib.decompress(self.buckets[key]).decode("utf-8")
                rows = [json.loads(line) for line in text.splitlines()]
            except (OSError, ValueError, TypeError, KeyError, zlib.error) as error:
                raise CatalogUnavailable("매장 카탈로그의 지역 색인을 읽을 수 없습니다.") from error
            self.verify(identity)
            self.region_cache[key] = rows
            if len(self.region_cache) > 3:
                self.region_cache.popitem(last=False)
            return rows


class PublishedCatalogRepository:
    def __init__(self, index, identity):
        self._index = index
        self._identity = identity
        self._preview_menus = {}

    def verify_current(self):
        self._index.verify(self._identity)

    def _cast_venue(self, row):
        try:
            return self._cast_venue_values(row)
        except (ValueError, TypeError, AttributeError, OverflowError) as error:
            raise CatalogUnavailable("매장 정보 형식을 확인해 주세요.") from error

    def _cast_venue_values(self, row):
        venue = CsvLazyRepository._cast_venue_row(self, row)
        # The team snapshot does not establish current opening hours.
        venue["_team_catalog"] = True
        # The legacy converter supplies fabricated fallback values for missing data.
        try:
            venue["rating"] = float(row["rating"]) if row.get("rating") else None
        except (TypeError, ValueError):
            venue["rating"] = None
        if venue["rating"] is not None and (not isfinite(venue["rating"]) or not 0 <= venue["rating"] <= 5):
            venue["rating"] = None
        try:
            venue["review_count"] = int(float(row["review_count"])) if row.get("review_count") else None
        except (TypeError, ValueError, OverflowError):
            venue["review_count"] = None
        if venue["review_count"] is not None and venue["review_count"] < 0:
            venue["review_count"] = None
        phone = re.search(r"전화:([0-9\-]+)", row.get("좌석_및_편의정보") or "")
        venue["phone"] = (row.get("전화번호") or row.get("phone") or (phone.group(1) if phone else "")).strip()
        venue["open_hours"] = (row.get("영업시간") or row.get("open_hours") or "").strip()
        venue["open_time"] = (row.get("오픈시각") or row.get("open_time") or "").strip()
        venue["close_time"] = (row.get("마감시각") or row.get("close_time") or "").strip()
        venue["latitude"] = coordinate(row.get("위도_lat") or row.get("latitude"))
        venue["longitude"] = coordinate(row.get("경도_lng") or row.get("longitude"))
        if venue["latitude"] is not None and not -90 <= venue["latitude"] <= 90:
            venue["latitude"] = None
        if venue["longitude"] is not None and not -180 <= venue["longitude"] <= 180:
            venue["longitude"] = None
        budget = str(row.get("1인_평균예산(원)") or row.get("avg_price") or "").replace(",", "").strip()
        if not budget.isdecimal() or int(budget) <= 0:
            venue["avg_price"] = None
            venue["price_tier"] = None
        venue["recommendation_style"] = (row.get("추천음악_스타일") or row.get("recommendation_style") or "").strip()
        venue["추천음악_스타일"] = venue["recommendation_style"]
        venue["music_mood"] = (row.get("music_mood") or "").strip().upper()
        province, district = source_region(row)
        if province == "인천광역시" and district in {"중구", "서구"}:
            venue["gu_name"] = f"인천 {district}"
        return venue

    def _region_keys(self, region):
        province, district = requested_region(region)
        return [
            key for key in self._index.buckets
            if (province is None or key[0] == province)
            and (key[1] == district or (district.endswith("시") and key[1].startswith(district) and key[1].endswith("구")))
        ]

    def _venues_from_keys(self, keys, bounds=None):
        venues = []
        for key in keys:
            for row in self._index.region_rows(self._identity, key):
                if bounds is not None:
                    latitude = coordinate(row.get("위도_lat") or row.get("latitude"))
                    longitude = coordinate(row.get("경도_lng") or row.get("longitude"))
                    if (latitude is None or longitude is None or not bounds[0] <= latitude <= bounds[1]
                            or not bounds[2] <= longitude <= bounds[3]):
                        continue
                venues.append(self._cast_venue(row))
        self._preload_previews([row["venue_id"] for row in venues])
        self.verify_current()
        return venues

    def get_venues_by_gu(self, gu_name):
        return self._venues_from_keys(self._region_keys(gu_name))

    def get_venues_by_bbox(self, min_lat, max_lat, min_lng, max_lng, fallback_gu="강남구"):
        return self._venues_from_keys(self._index.buckets, (min_lat, max_lat, min_lng, max_lng))

    def _preload_previews(self, venue_ids):
        if not venue_ids:
            return
        self.verify_current()
        try:
            selected = {venue_id: ([], []) for venue_id in venue_ids}
            seen = set()
            with closing(self._index.connect()) as db:
                for start in range(0, len(venue_ids), 800):
                    batch = venue_ids[start:start + 800]
                    placeholders = ",".join("?" for _ in batch)
                    for menu_id, venue_id, raw in db.execute(
                        f"SELECT menu_id, venue_id, raw FROM menus WHERE venue_id IN ({placeholders})", batch
                    ):
                        item = json.loads(raw)
                        if not menu_id or menu_id in seen or item.get("venue_id") != venue_id or item.get("menu_id") != menu_id:
                            raise ValueError("매장 메뉴 ID 연결이 일치하지 않습니다.")
                        seen.add(menu_id)
                        menu = CsvLazyRepository._cast_menu_row(self, item)
                        signatures, others = selected[venue_id]
                        group = signatures if menu["is_signature"] else others
                        if len(group) < 6:
                            menu["price_confirmed"] = False
                            menu["menu_source"] = "TEAM_CSV"
                            group.append(menu)
            self.verify_current()
            self._preview_menus.update({
                venue_id: (signatures + others)[:6] for venue_id, (signatures, others) in selected.items()
            })
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error) as error:
            raise CatalogUnavailable("매장 메뉴를 읽을 수 없습니다.") from error

    def get_venue_by_id(self, venue_id):
        self.verify_current()
        try:
            with closing(self._index.connect()) as db:
                rows = db.execute("SELECT raw FROM venues WHERE venue_id = ? LIMIT 2", (venue_id,)).fetchall()
            self.verify_current()
            if len(rows) != 1:
                return None
            row = json.loads(rows[0][0])
            if row.get("venue_id") != venue_id:
                raise ValueError("매장 ID 연결이 일치하지 않습니다.")
            return self._cast_venue(row) if is_listable(row) else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error) as error:
            raise CatalogUnavailable("매장 상세를 읽을 수 없습니다.") from error

    def get_menus_by_venue(self, venue_id):
        self.verify_current()
        if self.get_venue_by_id(venue_id) is None:
            return []
        try:
            with closing(self._index.connect()) as db:
                rows = db.execute("SELECT menu_id, raw FROM menus WHERE venue_id = ?", (venue_id,))
                menus = []
                seen = set()
                for menu_id, raw in rows:
                    item = json.loads(raw)
                    if not menu_id or menu_id in seen or item.get("venue_id") != venue_id or item.get("menu_id") != menu_id:
                        raise ValueError("매장 메뉴 ID 연결이 일치하지 않습니다.")
                    seen.add(menu_id)
                    menu = CsvLazyRepository._cast_menu_row(self, item)
                    menu["price_confirmed"] = False
                    menu["menu_source"] = "TEAM_CSV"
                    menus.append(menu)
            self.verify_current()
            return menus
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, sqlite3.Error) as error:
            raise CatalogUnavailable("매장 메뉴를 읽을 수 없습니다.") from error

    def get_verified_menus_by_venue(self, venue_id):
        return self.get_menus_by_venue(venue_id)

    def get_preview_menus_by_venue(self, venue, target_tier=None, limit=6):
        self.verify_current()
        venue_id = venue.get("venue_id", "")
        if venue_id not in self._preview_menus:
            self._preload_previews([venue_id])
        return self._preview_menus.get(venue_id, [])[:limit]


@lru_cache(maxsize=2)
def _catalog_index(path):
    return _CatalogIndex(path)


def get_operational_venue_repo():
    configured = os.environ.get("VENUE_MCP_PUBLIC_CATALOG", "").strip()
    if not configured or Path(configured).suffix.lower() not in {".sqlite", ".sqlite3"}:
        raise CatalogUnavailable("운영 매장 카탈로그 경로가 설정되지 않았습니다.")
    return _catalog_index(configured).snapshot()
