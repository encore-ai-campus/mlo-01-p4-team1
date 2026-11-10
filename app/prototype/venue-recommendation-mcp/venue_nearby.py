"""Resolve Seoul place names using the registered public location index."""

from collections import defaultdict
from dataclasses import dataclass
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
import re
import unicodedata

from verify_tag_weather import normalize


DEFAULT_INDEX = Path(__file__).with_name("seoul_location_index.json")
RADIUS_M = 1000
# Lines with the same station name can represent different locations.
TRANSFER_MAX_SPAN_M = 600
CITY_ALIASES = {"서울", "서울시", "서울특별시", "서울 전체", "서울 전역"}
WORD_START = r"(?<![가-힣a-z0-9])"
WORD_END = r"(?=$|[^가-힣a-z0-9]|[0-9]{1,3}\s*(?:번\s*)?출구|근처|주변|인근|부근|일대|에서|으로|쪽|앞|카페|술집)"
STATION_LIKE = re.compile(WORD_START + r"([가-힣0-9]{1,18}역)" + WORD_END, re.IGNORECASE)
EXIT = re.compile(r"(?<![0-9])([0-9]{1,3})\s*(?:번\s*)?출구")
UNMAPPED_NEARBY = re.compile(r"([가-힣a-z0-9]{2,})\s*(?:근처|주변|인근|부근)", re.IGNORECASE)
NON_STATION_ENDINGS = ("지역", "구역", "권역", "영역", "전역", "방역", "번역", "내역")


def dong_key(name):
    return re.sub(r"제(?=[0-9]+동$)", "", normalize(name))


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

    def distance(self, coordinates):
        return min(straight_line_m(lat, lng, coordinates) for lat, lng in self.points)

    def includes(self, district, dong):
        return (self.kind == "CITY" or district == self.district) and (
            not self.dongs or dong_key(dong or "") in self.dongs
        )

    def public(self):
        return {
            "label": self.label,
            "kind": self.kind,
            **({"radius_m": RADIUS_M} if self.points else {}),
            **({"lat": self.points[0][0], "lng": self.points[0][1]} if self.kind == "EXIT" else {}),
            **({"source_label": self.source_label} if self.source_label else {}),
            **({"source_url": self.source_url} if self.source_url else {}),
            **({"unmatched_exit": self.unmatched_exit} if self.unmatched_exit is not None else {}),
        }


@dataclass(frozen=True)
class PlaceMatch:
    remaining_text: str
    area: SearchArea | None = None
    reason: str | None = None
    choices: tuple[dict[str, str], ...] = ()


class SeoulPlaceIndex:
    def __init__(self, path=DEFAULT_INDEX):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.sources = data["sources"]
        self.stations = defaultdict(list)
        for row in data["stations"]:
            self.stations[normalize(station_label(row["name"]))].append(row)
        self.exits = defaultdict(dict)
        for row in data["exits"]:
            self.exits[normalize(row["station"])][row["number"]] = row

        self.city_names = {normalize(name) for name in CITY_ALIASES}
        self.districts = {normalize(row["district"]): row["district"] for row in data["regions"]}
        self.dongs = defaultdict(lambda: defaultdict(set))
        for row in data["regions"]:
            name = dong_key(row["name"])
            aliases = {normalize(row["name"]), name}
            group = re.fullmatch(r"(.+?)(?:[0-9]+가)?[0-9]+동", name)
            base = group[1] if group else name.removesuffix("동")
            if len(base) >= 2:
                aliases.update((base, base + "동"))
            for alias in aliases:
                self.dongs[alias][row["district"]].add(name)

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

        aliases = self.city_names | self.districts.keys() | self.dongs.keys() | self.stations.keys() | self.landmarks.keys()
        patterns = [r"\s*".join(map(re.escape, alias)) for alias in sorted(aliases, key=lambda value: (-len(value), value))]
        self.name_pattern = re.compile(WORD_START + "(" + "|".join(patterns) + ")" + WORD_END, re.IGNORECASE)

    def _source(self, key):
        source = self.sources[key]
        return {"source_label": source["label"], "source_url": source.get("url")}

    def _station(self, key, original, remaining, match, number):
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
                source_label=self.sources["exit"]["label"], source_url=row["url"],
            ))

        if line_matches:
            label = f'{rows[0]["line"]} {label}'
        if exit_rows and not line_matches:
            points = tuple((row["lat"], row["lng"]) for row in exit_rows.values())
            source = self._source("exit")
        else:
            points = tuple((row["lat"], row["lng"]) for row in rows)
            source = self._source("station")
        return PlaceMatch(remaining, SearchArea(label, "STATION", points, unmatched_exit=number, **source))

    @staticmethod
    def _choices(original, remaining, match, labels):
        choices = tuple({
            "label": label,
            "query": original[:match.start()] + label + original[match.end():],
        } for label in labels)
        return PlaceMatch(remaining, reason="AMBIGUOUS_LOCATION", choices=choices)

    def resolve(self, query):
        original = unicodedata.normalize("NFKC", query)
        matches = {normalize(match[0]): match for match in self.name_pattern.finditer(original)}
        remaining = self.name_pattern.sub(" ", original).strip()
        exits = EXIT.findall(original)
        station_matches = [key for key in matches if key in self.stations]
        if len(station_matches) > 1 or len(exits) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        if station_matches:
            key, = station_matches
            return self._station(key, original, remaining, matches[key], int(exits[0]) if exits else None)

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
                row["name"], "LANDMARK", ((row["lat"], row["lng"]),), **self._source("landmark"),
            ))

        districts = {self.districts[key] for key in matches if key in self.districts}
        dong_matches = [key for key in matches if key in self.dongs]
        if len(districts) > 1 or len(dong_matches) > 1:
            return PlaceMatch(remaining, reason="MULTIPLE_LOCATIONS")
        if dong_matches:
            key, = dong_matches
            groups = {district: names for district, names in self.dongs[key].items()
                      if not districts or district in districts}
            if not groups:
                return PlaceMatch(remaining, reason="LOCATION_CONFLICT")
            labels = {
                district: f"{district} {next(iter(names)) if len(names) == 1 else key.removesuffix('동') + '동'}"
                for district, names in groups.items()
            }
            if len(groups) > 1:
                return self._choices(original, remaining, matches[key], sorted(labels.values()))
            district, = groups
            return PlaceMatch(remaining, SearchArea(labels[district], "DONG", district=district,
                                                    dongs=frozenset(groups[district]), **self._source("region")))
        if districts:
            district, = districts
            return PlaceMatch(remaining, SearchArea(district, "DISTRICT", district=district, **self._source("region")))
        if matches.keys() & self.city_names:
            return PlaceMatch(remaining, SearchArea("서울", "CITY"))
        if UNMAPPED_NEARBY.search(original):
            return PlaceMatch(remaining, reason="PLACE_UNSUPPORTED")
        return PlaceMatch(original)
