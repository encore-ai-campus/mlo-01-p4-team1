"""Read the original team venue and menu rows from a read-only SQLite index."""

from collections import defaultdict
from contextlib import closing
import gzip
import hashlib
from io import BytesIO, TextIOWrapper
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock
from urllib.parse import urlsplit

from prepare_team_catalog import _reviewed_menu_names, _reviewed_moods, menu_classifier
from venue_csv_catalog import is_out_of_scope_facility, positive_won
from venue_nearby import PROVINCE_ALIASES, PROVINCE_LABELS, csv_coordinates, district_contains, dong_key
from venue_public_catalog import VenueResearch
from venue_visit_purposes import reviewed_venue_tags
from verify_tag_weather import normalize


class TeamCatalogReader:
    def __init__(self, path, weather_regions, tags, labels, ancestors):
        self.path = Path(path)
        self.weather_regions = weather_regions
        self.tags = tags
        self.labels = labels
        self.classify_menu = menu_classifier(labels, ancestors)
        self.lock = Lock()
        self.cached = None

    def connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)

    def identity(self):
        info = self.path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def read(self):
        with self.lock:
            identity = self.identity()
            if self.cached and self.cached[0] == identity:
                return self.cached[1]
            with closing(self.connect()) as db:
                item = db.execute("SELECT value FROM metadata WHERE key = 'catalog'").fetchone()
                metadata = json.loads(item[0]) if item else {}
                if metadata.get("schema") != 1 or metadata.get("preview_only") is not False:
                    raise ValueError("운영 팀 카탈로그 형식을 확인해 주세요.")
                venues, locations, seen = [], {}, set()
                cached_rows = db.execute("SELECT value FROM metadata WHERE key = 'venue_rows_gzip'").fetchone()
                if not cached_rows:
                    raise ValueError("팀 카탈로그의 매장 색인이 없습니다.")
                stream = TextIOWrapper(gzip.GzipFile(fileobj=BytesIO(cached_rows[0])), encoding="utf-8")
                for content in stream:
                    raw = json.loads(content)
                    venue_id = raw.get("venue_id", "").strip()
                    if not venue_id or venue_id in seen:
                        raise ValueError("팀 카탈로그의 매장 ID가 비어 있거나 중복됩니다.")
                    seen.add(venue_id)
                    category = {"카페": "CAFE", "술집": "PUB", "주점": "PUB"}.get(raw.get("업종구분", "").strip())
                    name = raw.get("매장명", "").strip()
                    address = raw.get("도로명주소", "").strip()
                    if (not category or not name or not address or is_out_of_scope_facility(name)
                            or raw.get("영업상태_코드", "").strip() not in {"OPEN", "1"}):
                        continue
                    province = PROVINCE_ALIASES.get(address.split()[0])
                    if province not in PROVINCE_LABELS:
                        continue
                    district, dong = raw.get("자치구", "").strip(), raw.get("행정동", "").strip()
                    region_id = "region:" + raw.get("행정동코드", "").strip()
                    region = self.weather_regions.get(region_id)
                    location = {"region_id": region_id, "province": province, "district": district, "admin_dong": dong}
                    if (region and region["sido"] == province and district_contains(region["sigungu"], district)
                            and dong_key(region["name"]) == dong_key(dong)):
                        location.update(nx=region["nx"], ny=region["ny"])
                    point = csv_coordinates(raw)
                    if point:
                        location.update(latitude=point[0], longitude=point[1])
                    moods = {
                        self.labels["MOOD"][label]
                        for word in re.split(r"[|;,]", raw.get("매장분위기", ""))
                        if (label := normalize(word)) in self.labels["MOOD"]
                    }
                    venue = {
                        "venue_id": venue_id, "name": name, "address": address,
                        "venue_category": category, "sub_category": raw.get("세부카테고리", "").strip() or None,
                        "province": province, "district": district, "admin_dong": dong,
                        "mood_tag_ids": sorted(moods), "avg_budget_krw": positive_won(raw.get("1인_평균예산(원)", "")),
                    }
                    if point:
                        venue["coordinates"] = point
                    sources = []
                    for value in raw.get("출처_URL", "").split("|"):
                        url = value.strip()
                        parsed = urlsplit(url)
                        if parsed.scheme in {"http", "https"} and parsed.netloc:
                            sources.append(url)
                    if sources:
                        research = {
                            "description": raw.get("매장특징_조사", ""),
                            "seating": raw.get("좌석_편의_조사", ""),
                            "sources": sources,
                            "limitations": raw.get("제한사항", ""),
                            "checked_on": raw.get("조사확인일", ""),
                        }
                        purposes, reviewed_moods = reviewed_venue_tags(category, VenueResearch.model_validate(research))
                        venue["research"] = research
                        venue["visit_purposes"] = sorted(purposes)
                        venue["mood_tag_ids"] = sorted(
                            moods | reviewed_moods | set(_reviewed_moods(raw.get("매장분위기_조사", ""), self.labels["MOOD"]))
                        )
                        venue["_research_menu_names"] = _reviewed_menu_names(raw.get("대표메뉴_조사", ""))
                    venues.append(venue)
                    locations[venue_id] = location
            if self.identity() != identity:
                raise ValueError("팀 카탈로그를 읽는 중 파일이 변경되었습니다.")
            result = (
                {"version": metadata["version"], "venues": venues, "menus": [], "_identity": identity,
                 "metadata": {key: metadata[key] for key in ("preview_only", "source_basis", "budget_basis", "tag_basis")}},
                locations,
            )
            self.cached = identity, result
            return result

    def menus_for(self, venues, expected_identity):
        if not venues:
            return []
        identity = self.identity()
        if identity != expected_identity:
            raise ValueError("팀 카탈로그가 교체되어 요청을 다시 실행해야 합니다.")
        ids = {venue["venue_id"] for venue in venues}
        research = {venue["venue_id"]: venue for venue in venues if venue.get("research")}
        names_seen = defaultdict(set)
        menus, seen_ids, classified = [], set(), {}
        with closing(self.connect()) as db:
            ordered = sorted(ids)
            for start in range(0, len(ordered), 800):
                batch = ordered[start:start + 800]
                placeholders = ",".join("?" for _ in batch)
                for menu_id, venue_id, content in db.execute(
                    f"SELECT menu_id, venue_id, raw FROM menus WHERE venue_id IN ({placeholders})", batch
                ):
                    if not menu_id or menu_id in seen_ids:
                        raise ValueError("팀 카탈로그의 메뉴 ID가 비어 있거나 중복됩니다.")
                    seen_ids.add(menu_id)
                    raw = json.loads(content)
                    name = raw.get("메뉴명", "").strip()
                    if not name:
                        continue
                    name_key = normalize(name)
                    names_seen[venue_id].add(name_key)
                    if name not in classified:
                        classified[name] = self.classify_menu(name)
                    menu = {
                        "menu_id": menu_id, "venue_id": venue_id, "name": name,
                        "price_krw": positive_won(raw.get("가격(원)", "")),
                        "menu_type_tag_ids": classified[name], "cuisine_tag_ids": [],
                    }
                    parent = research.get(venue_id)
                    if parent and name_key in {normalize(value) for value in parent["_research_menu_names"]}:
                        menu["evidence"] = {"urls": parent["research"]["sources"]}
                    menus.append(menu)
        for venue_id, venue in research.items():
            for name in venue["_research_menu_names"]:
                if (not name or len(name) > 40 or "\n" in name
                        or re.search(r"\d[\d,]*\s*원|미확인|확인\s*필요|불명|없음", name)
                        or normalize(name) in names_seen[venue_id]):
                    continue
                digest = hashlib.sha256((venue_id + "\0" + name).encode("utf-8")).hexdigest()[:20]
                menu_id = "research-" + digest
                if menu_id in seen_ids:
                    raise ValueError("조사 메뉴 ID가 기존 메뉴와 충돌합니다.")
                seen_ids.add(menu_id)
                names_seen[venue_id].add(normalize(name))
                menus.append({
                    "menu_id": menu_id, "venue_id": venue_id, "name": name, "price_krw": None,
                    "menu_type_tag_ids": self.classify_menu(name), "cuisine_tag_ids": [],
                    "evidence": {"urls": venue["research"]["sources"]},
                })
        if self.identity() != identity:
            raise ValueError("팀 카탈로그를 읽는 중 파일이 변경되었습니다.")
        return menus
