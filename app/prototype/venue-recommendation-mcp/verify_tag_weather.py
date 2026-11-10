"""공통 태그와 실매장 자료로 메뉴·분위기 및 우천 좌석 매칭을 검증합니다."""

import csv
import json
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


TAG_KINDS = ("MOOD", "CUISINE", "MENU_TYPE")
WEATHER = ("CLEAR", "RAIN", "NO_FORECAST")
SEATING = ("ANY", "PREFER_OUTDOOR", "REQUIRE_OUTDOOR")
SEAT_LABELS = {
    "OUTDOOR": "야외석",
    "INDOOR_CONFIRMED": "실내석 확인",
    "INDOOR_FALLBACK": "야외 휴장 · 실내 대체",
    "INDOOR_OUTDOOR_UNVERIFIED": "실내석 확인 · 야외 우천 운영 미확인",
    "OUTDOOR_RAIN_UNVERIFIED": "야외 우천 운영 미확인",
    "OUTDOOR_WEATHER_UNKNOWN": "예보 없음 · 야외 운영 미판정",
    "OUTDOOR_CLOSED_RAIN": "비로 야외석 휴장",
    "NO_USABLE_SEATS_RAIN": "비에 이용 가능한 좌석 없음",
    "WEATHER_NOT_APPLIED": "날씨 보정 없음",
    "SEATING_UNVERIFIED": "좌석 운영 미확인",
}


def normalize(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def read_tags(path):
    tags = {}
    labels = {kind: {} for kind in TAG_KINDS}
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            tag_id, kind = row["tag_id"], row["kind"]
            if tag_id in tags or kind not in labels:
                raise ValueError(f"잘못된 태그 정의 {tag_id} {kind}")
            tags[tag_id] = row
            for label in [tag_id, row["label_ko"], *row["aliases"].split("|")]:
                if label and labels[kind].setdefault(normalize(label), tag_id) != tag_id:
                    raise ValueError(f"태그 별칭 충돌 {label}")

    ancestors = {}
    for tag_id, row in tags.items():
        chain = {tag_id}
        parent = row["parent_tag_id"]
        while parent:
            if parent in chain or parent not in tags or tags[parent]["kind"] != row["kind"]:
                raise ValueError(f"상위 태그 연결 오류 {tag_id} {parent}")
            chain.add(parent)
            parent = tags[parent]["parent_tag_id"]
        ancestors[tag_id] = chain
    return tags, labels, ancestors


def verify_dump(path, tags, labels):
    candidates = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    keys = {(row["kind"], normalize(row["label_ko"])) for row in candidates}
    if len(keys) != len(candidates):
        raise ValueError("같은 유형과 이름의 후보가 중복되었습니다.")
    accepted = {row["tag_id"]: row for row in candidates if row["decision"] == "USE"}
    if accepted.keys() != tags.keys():
        raise ValueError("후보 선별본과 공통 목록의 태그 ID가 다릅니다.")
    for tag_id, candidate in accepted.items():
        fields = ("kind", "label_ko", "parent_tag_id")
        if not candidate["examples"] or any(candidate[field] != tags[tag_id][field] for field in fields):
            raise ValueError(f"공통 목록의 근거 또는 분류가 일치하지 않습니다. {tag_id}")
        if "|".join(candidate["aliases"]) != tags[tag_id]["aliases"]:
            raise ValueError(f"공통 목록의 별칭이 일치하지 않습니다. {tag_id}")
    for candidate in candidates:
        if candidate["decision"] == "ALIAS":
            tag_id = labels[candidate["kind"]].get(normalize(candidate["label_ko"]))
            if tag_id is None or tag_id != candidate["target_tag_id"]:
                raise ValueError(f"별칭 연결 오류 {candidate['label_ko']}")
    return Counter(row["decision"] for row in candidates)


def verify_pilot(pilot, tags):
    venues, menus = pilot["venues"], pilot["menus"]
    venue_ids = {venue["venue_id"] for venue in venues}
    if len(venue_ids) != len(venues) or len({menu["menu_id"] for menu in menus}) != len(menus):
        raise ValueError("시범 매장 또는 메뉴 ID가 중복됩니다.")

    for records, key, field, kind in (
        (venues, "venue_id", "mood_tag_ids", "MOOD"),
        (menus, "menu_id", "cuisine_tag_ids", "CUISINE"),
        (menus, "menu_id", "menu_type_tag_ids", "MENU_TYPE"),
    ):
        for record in records:
            for tag_id in record[field]:
                if tag_id not in tags or tags[tag_id]["kind"] != kind:
                    raise ValueError(f"{record[key]}의 {tag_id}를 {kind}에 등록할 수 없습니다.")

    for venue in venues:
        if set(venue["mood_tag_ids"]) != set(venue["mood_evidence"]):
            raise ValueError(f"분위기 근거가 일치하지 않습니다. {venue['name']}")
        if any(not item["urls"] for item in venue["mood_evidence"].values()):
            raise ValueError(f"분위기 근거 링크가 없습니다. {venue['name']}")
        seats = venue["seats"]
        for kind in ("indoor", "outdoor"):
            if seats[kind] not in ("CONFIRMED", "ABSENT", "UNKNOWN"):
                raise ValueError(f"좌석 상태 오류 {venue['name']} {kind}")
            if seats[kind] != "UNKNOWN" and not seats["evidence"].get(kind, {}).get("urls"):
                raise ValueError(f"좌석 상태의 근거 링크가 없습니다. {venue['name']} {kind}")
        if seats["outdoor_in_rain"] not in ("OPEN", "CLOSED", "UNKNOWN"):
            raise ValueError(f"우천 운영 상태 오류 {venue['name']}")
        if seats["outdoor_in_rain"] != "UNKNOWN":
            support = seats["evidence"].get("outdoor_in_rain", {})
            if seats["outdoor"] != "CONFIRMED" or not support.get("urls"):
                raise ValueError(f"야외 우천 상태의 근거 링크가 없습니다. {venue['name']}")
            if support.get("source_level") != "OPERATOR_POLICY":
                raise ValueError(f"시범의 우천 확정에는 운영사 정책 근거가 필요합니다. {venue['name']}")

    for menu in menus:
        if menu["venue_id"] not in venue_ids or not menu["menu_type_tag_ids"] or not menu["evidence"]["urls"]:
            raise ValueError(f"메뉴의 매장, 유형 또는 근거 링크가 없습니다. {menu['menu_id']}")


def resolve(value, labels, kind):
    tag_id = labels[kind].get(normalize(value))
    if tag_id is None:
        raise ValueError(f"{kind} 목록에서 ‘{value}’ 태그를 찾을 수 없습니다.")
    return tag_id


def expand(tag_ids, ancestors):
    return {tag for tag_id in tag_ids for tag in ancestors[tag_id]}


def seat_result(seats, seating, weather):
    outdoor = seats["outdoor"] == "CONFIRMED"
    rain_policy = seats["outdoor_in_rain"]
    outdoor_open = outdoor and (weather == "CLEAR" or (weather == "RAIN" and rain_policy == "OPEN"))

    if seating == "REQUIRE_OUTDOOR":
        if not outdoor:
            return None, None, 0
        if weather == "NO_FORECAST":
            return "ranked", "OUTDOOR_WEATHER_UNKNOWN", 0
        if outdoor_open:
            return "ranked", "OUTDOOR", 0
        if rain_policy == "CLOSED":
            return "excluded", "OUTDOOR_CLOSED_RAIN", 0
        return "pending", "OUTDOOR_RAIN_UNVERIFIED", 0

    if weather == "NO_FORECAST":
        return "ranked", "WEATHER_NOT_APPLIED", 0
    if outdoor_open and seating == "PREFER_OUTDOOR":
        return "ranked", "OUTDOOR", 1
    if seats["indoor"] == "CONFIRMED":
        if weather == "RAIN" and outdoor:
            if rain_policy == "CLOSED":
                return "ranked", "INDOOR_FALLBACK", 0
            if rain_policy == "UNKNOWN":
                return "ranked", "INDOOR_OUTDOOR_UNVERIFIED", 0
        return "ranked", "INDOOR_CONFIRMED", 0
    if outdoor_open:
        return "ranked", "OUTDOOR", 0
    if weather == "RAIN" and outdoor and rain_policy == "CLOSED" and seats["indoor"] == "ABSENT":
        return "excluded", "NO_USABLE_SEATS_RAIN", 0
    return "ranked", "SEATING_UNVERIFIED", 0


def recommend(pilot, query, labels, ancestors):
    weather, seating = query["weather"], query["seating"]
    if weather not in WEATHER or seating not in SEATING:
        raise ValueError("날씨 또는 좌석 선택 상태가 올바르지 않습니다.")
    preferred = {resolve(value, labels, "MOOD") for value in query["preferred_moods"]}
    orders = []
    for order in query["orders"]:
        required = {resolve(value, labels, "MENU_TYPE") for value in order["types"]}
        if order["cuisine"]:
            required.add(resolve(order["cuisine"], labels, "CUISINE"))
        orders.append(required)

    menus_by_venue = defaultdict(list)
    for menu in pilot["menus"]:
        menu_tags = expand(menu["cuisine_tag_ids"] + menu["menu_type_tag_ids"], ancestors)
        menus_by_venue[menu["venue_id"]].append((menu["menu_id"], menu_tags))

    decisions = {"ranked": [], "pending": [], "excluded": []}
    for venue in sorted(pilot["venues"], key=lambda row: row["venue_id"]):
        if query["district"] is not None and venue["district"] != query["district"]:
            continue
        venue_id = venue["venue_id"]
        menu_groups = [
            sorted(menu_id for menu_id, menu_tags in menus_by_venue[venue_id] if required <= menu_tags)
            for required in orders
        ]
        if not all(menu_groups):
            continue

        bucket, seat, outdoor_priority = seat_result(venue["seats"], seating, weather)
        if bucket is None:
            continue
        mood_ids = sorted(preferred & expand(venue["mood_tag_ids"], ancestors))
        result = {"venue_id": venue_id, "matched_mood_ids": mood_ids, "matched_menu_groups": menu_groups, "seat": seat}
        rank = (-len(mood_ids), -outdoor_priority, venue_id)
        decisions[bucket].append((rank, result))

    return {bucket: [result for _, result in sorted(rows)] for bucket, rows in decisions.items()}


def format_result(result, venue_names, menu_names):
    name = venue_names[result["venue_id"]]
    ordered_items = [" / ".join(menu_names[menu_id] for menu_id in group) for group in result["matched_menu_groups"]]
    details = " · ".join([*ordered_items, SEAT_LABELS[result["seat"]]])
    return f"{name} [{details}]"


def main():
    folder = Path(__file__).resolve().parent
    tags, labels, ancestors = read_tags(folder / "공통_태그_목록_v0.2.csv")
    counts = verify_dump(folder / "태그_후보_dump_v0.2.jsonl", tags, labels)
    pilot = json.loads((folder / "실매장_태그_날씨_시범_v0.2.json").read_text(encoding="utf-8"))
    verify_pilot(pilot, tags)
    venue_names = {row["venue_id"]: row["name"] for row in pilot["venues"]}
    menu_names = {row["menu_id"]: row["name"] for row in pilot["menus"]}

    for case in pilot["cases"]:
        result = recommend(pilot, case["query"], labels, ancestors)
        if result != case["expected"]:
            raise AssertionError(f"{case['name']}\n기대 {case['expected']}\n실제 {result}")
        ranked = [format_result(row, venue_names, menu_names) for row in result["ranked"]]
        details = [" → ".join(ranked) if ranked else "추천 결과 없음"]
        for bucket, label in (("pending", "확인 필요"), ("excluded", "제외")):
            if result[bucket]:
                names = " / ".join(format_result(row, venue_names, menu_names) for row in result[bucket])
                details.append(f"{label} {names}")
        print(f"PASS {case['name']} → {' | '.join(details)}")

    tag_counts = Counter(row["kind"] for row in tags.values())
    print(f"후보 {sum(counts.values())}건 · 사용 {counts['USE']} · 별칭 {counts['ALIAS']} · 보류 {counts['REVIEW']}")
    print(f"분위기 {tag_counts['MOOD']} · 요리 계열 {tag_counts['CUISINE']} · 메뉴 유형 {tag_counts['MENU_TYPE']}")
    print(f"실매장 {len(venue_names)}곳 · 메뉴 {len(menu_names)}개 · {len(pilot['cases'])}개 시나리오 통과")


if __name__ == "__main__":
    main()
