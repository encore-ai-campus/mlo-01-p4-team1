"""시연 CSV를 원본 venue_id로 추가합니다. 메뉴는 검증 카탈로그에서 읽습니다."""

import csv
import re

from venue_nearby import csv_coordinates
from venue_search_intent import CALM_WORDS
from verify_tag_weather import normalize


# '요리 주점'에는 식당도 들어 있으므로 업종 조건에 그대로 사용하지 않습니다.
CONFIRMED_PUB_SUBTYPES = {
    "생맥주전문", "살얼음맥주/꼬치주점", "대형호프/세계맥주", "수제맥주/치맥전문",
    "맥주전문점", "와인바", "칵테일바", "이자카야", "전통주점", "호프",
}
CLOSED_STATUSES = {"CLOSED", "CLOSED_HIDDEN"}


def csv_category(row):
    category = normalize(row["업종구분"])
    subtype = normalize(row["세부카테고리"])
    if category == "카페" and subtype == "카페":
        return "CAFE"
    if category in {"술집", "주점"} and subtype in CONFIRMED_PUB_SUBTYPES:
        return "PUB"
    return None


def positive_won(value):
    digits = value.strip().replace(",", "")
    if not (digits.isascii() and digits.isdecimal() and len(digits) <= 7):
        return None
    return int(digits) or None


def add_csv_venues(catalog, locations, path, mood_labels):
    venues = {row["venue_id"]: row for row in catalog["venues"]}
    closed = set()
    seen = set()
    labels = {**mood_labels, **{word: "mood_calm" for word in CALM_WORDS}}
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"venue_id", "매장명", "자치구", "행정동", "도로명주소", "업종구분", "세부카테고리",
                    "매장분위기", "영업상태_코드", "1인_평균예산(원)"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("매장 CSV에 필요한 헤더가 없습니다.")
        for raw in reader:
            row = {key: (value or "").strip() for key, value in raw.items() if key is not None}
            venue_id = row["venue_id"]
            if not venue_id or venue_id in seen:
                raise ValueError("매장 CSV의 venue_id가 비어 있거나 중복됩니다.")
            seen.add(venue_id)
            if row["영업상태_코드"] in CLOSED_STATUSES:
                closed.add(venue_id)
                continue
            if not row["매장명"] or not row["자치구"]:
                continue

            verified = venues.get(venue_id)
            if verified and (verified["district"] != row["자치구"] or verified["name"] != row["매장명"]):
                raise ValueError(f"매장 CSV와 검증 카탈로그의 매장 정보가 다릅니다. {venue_id}")
            mood_ids = sorted({
                labels[label] for word in re.split(r"[|;,]", row["매장분위기"])
                if (label := normalize(word)) in labels
            })
            venue = dict(verified) if verified else {
                "venue_id": venue_id, "name": row["매장명"], "district": row["자치구"],
                "admin_dong": row["행정동"], "address": row["도로명주소"], "mood_tag_ids": mood_ids,
            }
            venue.setdefault("venue_category", csv_category(row))
            venue.setdefault("sub_category", row["세부카테고리"])
            venue.setdefault("avg_budget_krw", positive_won(row["1인_평균예산(원)"]))
            coordinates = csv_coordinates(row)
            if coordinates:
                venue["coordinates"] = coordinates
            venues[venue_id] = venue

            # CSV의 행정동 코드를 그대로 사용합니다. 매핑이 없으면 예보 미확인입니다.
            region_code = row.get("행정동코드", "")
            nx = row.get("기상청격자_X", "")
            ny = row.get("기상청격자_Y", "")
            if re.fullmatch(r"[0-9]{10}", region_code) and all(re.fullmatch(r"[0-9]{1,3}", v) for v in (nx, ny)):
                locations.setdefault(venue_id, {
                    "region_id": f"region:{region_code}", "nx": int(nx), "ny": int(ny),
                    "district": venue["district"], "admin_dong": venue.get("admin_dong") or row["행정동"],
                })

    return {
        **catalog,
        "venues": [venue for venue_id, venue in venues.items() if venue_id not in closed],
        "menus": [menu for menu in catalog["menus"] if menu["venue_id"] not in closed],
    }
