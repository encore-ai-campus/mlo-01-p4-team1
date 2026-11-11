"""Resolve places using the project station, landmark and administrative data."""

from collections import defaultdict
from dataclasses import dataclass
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import re
import unicodedata

from verify_tag_weather import normalize


DEFAULT_INDEX = Path(__file__).with_name("seoul_location_index.json")
ADMIN_MAPPING = Path(__file__).resolve().parents[2] / "pipeline" / "weather" / "mapping.json"
RADIUS_M = 1000
# Lines with the same station name can represent different locations.
TRANSFER_MAX_SPAN_M = 600
SEOUL = "서울특별시"
PROVINCE_LABELS = {SEOUL: "서울", "인천광역시": "인천", "경기도": "경기도"}
PROVINCE_ALIASES = {
    "서울": SEOUL, "서울시": SEOUL, SEOUL: SEOUL,
    "인천": "인천광역시", "인천시": "인천광역시", "인천광역시": "인천광역시",
    "경기": "경기도", "경기도": "경기도",
}
CAPITAL_ALIASES = {"수도권", "수도권전체", "수도권전역"}
WORD_START = r"(?<![가-힣a-z0-9])"
WORD_END = r"(?=$|[^가-힣a-z0-9]|[0-9]{1,3}\s*(?:번\s*)?출구|근처|주변|인근|부근|일대|에서|으로|에(?=$|[^가-힣a-z0-9]|있는)|쪽|앞|카페|술집)"
STATION_LIKE = re.compile(WORD_START + r"([가-힣0-9]{1,18}역)" + WORD_END, re.IGNORECASE)
EXIT = re.compile(r"(?<![0-9])([0-9]{1,3})\s*(?:번\s*)?출구")
UNMAPPED_NEARBY = re.compile(r"([가-힣a-z0-9]{2,})\s*(?:근처|주변|인근|부근)", re.IGNORECASE)
NON_STATION_ENDINGS = ("지역", "구역", "권역", "영역", "전역", "방역", "번역", "내역")


def dong_key(name):
    return re.sub(r"제(?=[0-9]+동$)", "", normalize(name))


def district_contains(district, selected):
    name, scope = normalize(district), normalize(selected)
    return name == scope or (scope.endswith("시") and name.startswith(scope) and name.endswith("구"))


def display_district(district):
    return re.sub(r"^(.+?시)([^ ]+구)$", r"\1 \2", district)


def station_label(name):
    base = name.split("(", 1)[0].strip()
    return base if base.endswith("역") else base + "역"


def csv_coordinates(row):
    try:
        lat, lng = float(row.get("위도_lat", "")), float(row.get("경도_lng", ""))
    except ValueError:
        return None
    if 37 <= lat <= 38 and 126 <= lng <= 128:
        return lat, lng
    return None


def straight_line_m(origin_lat, origin_lng, coordinates):
    lat, lng = coordinates
    start = radians(origin_lat)
    end = radians(lat)
    delta_lng = radians(lng - origin_lng)
    h = sin((end - start) / 2) ** 2 + cos(start) * cos(end) * sin(delta_lng / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(h))


@dataclass(frozen=True)
class SearchArea:
    label: str
    kind: str
    points: tuple[tuple[float, float], ...] = ()
    district: str | None = None
    dongs: frozenset[str] = frozenset()
    source_label: str | None = None
    source_url: str | None = None
    unmatched_exit: int | None = None
    province: str | None = None

    def distance(self, coordinates):
        return min(straight_line_m(lat, lng, coordinates) for lat, lng in self.points)

    def includes(self, district, dong, province=None):
        return (self.province is None or self.province == province) and (
            self.district is None or district_contains(district or "", self.district)
        ) and (not self.dongs or dong_key(dong or "") in self.dongs)

    def public(self):
        return {
            "label": self.label,
            "kind": self.kind,
            **({"radius_m": RADIUS_M} if self.points else {}),
            **({"lat": self.points[0][0], "lng": self.points[0][1]} if self.kind == "EXIT" else {}),
            **({"source_label": self.source_label} if self.source_label else {}),
            **({"source_url": self.source_url} if self.source_url else {}),
            **({"unmatched_exit": self.unmatched_exit} if self.unmatched_exit is not None else {}),
            **({"province": self.province} if self.province else {}),
        }


@dataclass(frozen=True)
class PlaceMatch:
    remaining_text: str
    area: SearchArea | None = None
    reason: str | None = None
    choices: tuple[dict[str, str], ...] = ()


class SeoulPlaceIndex:
    def __init__(self, path=DEFAULT_INDEX, *, capital=False):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.capital = capital
        self.sources = data["sources"]
        self.stations = defaultdict(list)
        for row in data["stations"]:
            self.stations[normalize(station_label(row["name"]))].append(row)
        self.exits = defaultdict(dict)
        for row in data["exits"]:
            self.exits[normalize(row["station"])][row["number"]] = row

        self.city_names = {
            alias + suffix: province
            for alias, province in PROVINCE_ALIASES.items()
            for suffix in ("", "전체", "전역")
        }
        self.districts = {normalize(row["district"]): row["district"] for row in data["regions"]}
        self.region_labels = {"서울", *self.districts.values()}
        self.district_groups = defaultdict(set)
        self.dongs = defaultdict(lambda: defaultdict(set))
        if capital:
            self.region_labels = {"수도권", *PROVINCE_LABELS.values()}
            mapping = json.loads(ADMIN_MAPPING.read_text(encoding="utf-8"))
            self.weather_mapping_version = mapping["version"]
            self.weather_regions = {f"region:{row['region_code']}": row for row in mapping["regions"]}
            regions = (
                (row["sido"], row["sigungu"], row["name"]) for row in mapping["regions"]
                if row["sido"] in PROVINCE_LABELS and row["sigungu"]
            )
        else:
            regions = ((None, row["district"], row["name"]) for row in data["regions"])
        for province, district, locality in regions:
            self._index_region(province, district, locality)

        self.landmarks = defaultdict(dict)
        for row in data["landmarks"]:
            name = row["name"]
            base = name.split("(", 1)[0].strip()
            if normalize(base) in self.stations:
                continue
            aliases = {name, base, *base.split("·")}
            if "(" in name and name.endswith(")"):
                aliases.add(name.split("(", 1)[1][:-1])
            for suffix in ("공원", "관광특구"):
                if base.endswith(suffix) and len(base.removesuffix(suffix).strip()) >= 2:
                    aliases.add(base.removesuffix(suffix).strip())
            if base.endswith("카페거리") and normalize(base.removesuffix("카페거리")) in self.dongs:
                aliases.add(base.removesuffix("카페거리") + "동카페거리")
            for alias in aliases:
                if len(normalize(alias)) >= 2:
                    self.landmarks[normalize(alias)][row["code"]] = row

        aliases = (
            self.city_names.keys() | CAPITAL_ALIASES | self.district_groups.keys()
            | self.dongs.keys() | self.stations.keys() | self.landmarks.keys()
        )
        patterns = [r"\s*".join(map(re.escape, alias)) for alias in sorted(aliases, key=lambda value: (-len(value), value))]
        self.name_pattern = re.compile(WORD_START + "(" + "|".join(patterns) + ")" + WORD_END, re.IGNORECASE)
        province_patterns = "|".join(sorted(PROVINCE_ALIASES, key=len, reverse=True))
        self.joined_province = re.compile(WORD_START + f"({province_patterns})(?=[가-힣])")

    def _district_label(self, province, district):
        prefix = f"{PROVINCE_LABELS[province]} " if province else ""
        return prefix + display_district(district)

    def _index_region(self, province, district, locality):
        self.district_groups[normalize(district)].add((province, district))
        if province:
            self.region_labels.add(self._district_label(province, district))
            city_district = re.fullmatch(r"(.+?시)(.+구)", district)
            if city_district:
                city, subdivision = city_district.groups()
                self.district_groups[normalize(subdivision)].add((province, district))
                self.district_groups[normalize(city)].add((province, city))
                self.district_groups[normalize(city[:-1])].add((province, city))
                self.region_labels.add(self._district_label(province, city))
            elif province != SEOUL and district.endswith(("시", "군", "구")) and len(district) > 2:
                self.district_groups[normalize(district[:-1])].add((province, district))

        name = dong_key(locality)
        aliases = {normalize(locality), name}
        group = re.fullmatch(r"(.+?)(?:[0-9]+가)?[0-9]+동", name)
        base = group[1] if group else name.removesuffix("동")
        if len(base) >= 2:
            aliases.update((base, base + "동"))
        for alias in aliases:
            self.dongs[alias][province, district].add(name)

    def _source(self, key):
        source = self.sources[key]
        return {"source_label": source["label"], "source_url": source.get("url")}

    def _station(self, key, original, remaining, match, number, province=None):
        rows = self.stations[key]
        label = station_label(rows[0]["name"])
        line_matches = [row for row in rows if normalize(row["line"]) and normalize(row["line"]) in normalize(original)]
        if line_matches:
            longest = max(len(normalize(row["line"])) for row in line_matches)
            rows = [row for row in line_matches if len(normalize(row["line"])) == longest]

        if any(straight_line_m(a["lat"], a["lng"], (b["lat"], b["lng"])) > TRANSFER_MAX_SPAN_M
               for a in rows for b in rows):
            labels = sorted({f'{row["line"]} {label}' for row in rows})
            return self._choices(original, remaining, match, labels)

        exit_rows = self.exits.get(key, {})
        if number is not None and exit_rows:
            if number not in exit_rows:
                return PlaceMatch(remaining, reason="EXIT_UNSUPPORTED")
            row = exit_rows[number]
            return PlaceMatch(remaining, SearchArea(
                f"{label} {number}번 출구", "EXIT", ((row["lat"], row["lng"]),),
                source_label=self.sources["exit"]["label"], source_url=row["url"], province=province,
            ))

        if line_matches:
            label = f'{rows[0]["line"]} {label}'
        if exit_rows and not line_matches:
            points = tuple((row["lat"], row["lng"]) for row in exit_rows.values())
            source = self._source("exit")
        else:
            points = tuple((row["lat"], row["lng"]) for row in rows)
            source = self._source("station")
        return PlaceMatch(remaining, SearchArea(
            label, "STATION", points, unmatched_exit=number, province=province, **source,
        ))

    @staticmethod
    def _choices(original, remaining, match, labels):
        choices = tuple({
            "label": label,
            "query": original[:match.start()] + label + original[match.end():],
        } for label in labels)
        return PlaceMatch(remaining, reason="AMBIGUOUS_LOCATION", choices=choices)

    def resolve(self, query):
        original = unicodedata.normalize("NFKC", query)
        original = self.joined_province.sub(
            lambda match: match[0] + (" " if self.name_pattern.match(original[match.end():]) else ""),
            original,
        )
        matches = {normalize(match[0]): match for match in self.name_pattern.finditer(original)}
        remaining = self.name_pattern.sub(" ", original).strip()
        provinces = {self.city_names[key] for key in matches if key in self.city_names}
        if len(provinces) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        requested_province = next(iter(provinces), None)
        if not self.capital and (provinces - {SEOUL} or matches.keys() & CAPITAL_ALIASES):
            return PlaceMatch(remaining, reason="REGION_UNSUPPORTED")
        province = requested_province if self.capital else None
        exits = EXIT.findall(original)
        station_matches = [key for key in matches if key in self.stations]
        if len(station_matches) > 1 or len(exits) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        if station_matches:
            key, = station_matches
            return self._station(key, original, remaining, matches[key], int(exits[0]) if exits else None, province)

        if any(not match[1].endswith(NON_STATION_ENDINGS) and
               not any(known.start() <= match.start() < known.end() for known in matches.values())
               for match in STATION_LIKE.finditer(original)):
            return PlaceMatch(remaining, reason="STATION_UNSUPPORTED")
        if exits:
            return PlaceMatch(remaining, reason="STATION_REQUIRED")

        landmark_rows = {
            code: row for key in matches if key in self.landmarks and not (key.endswith("동") and key in self.dongs)
            for code, row in self.landmarks[key].items()
        }
        if len(landmark_rows) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        if landmark_rows:
            row, = landmark_rows.values()
            return PlaceMatch(remaining, SearchArea(
                row["name"], "LANDMARK", ((row["lat"], row["lng"]),), province=province, **self._source("landmark"),
            ))

        district_keys = [key for key in matches if key in self.district_groups]
        district_options = [
            {area for area in self.district_groups[key] if province is None or area[0] == province}
            for key in district_keys
        ]
        if any(not options for options in district_options):
            return PlaceMatch(remaining, reason="LOCATION_CONFLICT")
        districts = {
            (area_province, district) for options in district_options for area_province, district in options
            if all(any(area_province == selected_province and district_contains(district, selected)
                       for selected_province, selected in options) for options in district_options)
        }
        if district_options and not districts:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        dong_matches = [key for key in matches if key in self.dongs and key not in self.district_groups]
        if len(dong_matches) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        if dong_matches:
            key, = dong_matches
            groups = {
                (area_province, district): names
                for (area_province, district), names in self.dongs[key].items()
                if (province is None or area_province == province) and (
                    not districts or any(area_province == selected_province and district_contains(district, selected)
                                         for selected_province, selected in districts)
                )
            }
            if not groups:
                return PlaceMatch(remaining, reason="LOCATION_CONFLICT")
            labels = {
                area: f"{self._district_label(None if province else area[0], area[1])} "
                      f"{next(iter(names)) if len(names) == 1 else key.removesuffix('동') + '동'}"
                for area, names in groups.items()
            }
            if len(groups) > 1:
                return self._choices(original, remaining, matches[key], sorted(labels.values()))
            area, = groups
            return PlaceMatch(remaining, SearchArea(
                self._district_label(*area) + " " + labels[area].split(" ")[-1],
                "DONG", district=area[1], dongs=frozenset(groups[area]), province=area[0], **self._source("region"),
            ))
        if districts:
            if len(districts) > 1:
                labels = sorted(self._district_label(None if province else item[0], item[1]) for item in districts)
                return self._choices(original, remaining, matches[district_keys[-1]], labels)
            district_province, district = next(iter(districts))
            return PlaceMatch(remaining, SearchArea(
                self._district_label(district_province, district), "DISTRICT",
                district=district, province=district_province, **self._source("region"),
            ))
        if requested_province:
            return PlaceMatch(remaining, SearchArea(PROVINCE_LABELS[requested_province], "CITY", province=province))
        if matches.keys() & CAPITAL_ALIASES:
            return PlaceMatch(remaining, SearchArea("수도권", "CITY"))
        if UNMAPPED_NEARBY.search(original):
            return PlaceMatch(remaining, reason="PLACE_UNSUPPORTED")
        return PlaceMatch(original)
