"""Convert paired team CSV files to a local preview snapshot without API calls."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
from io import StringIO
import json
from math import isfinite
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


from venue_csv_catalog import CONFIRMED_PUB_SUBTYPES
from venue_nearby import PROVINCE_ALIASES, PROVINCE_LABELS, SeoulPlaceIndex, district_contains, dong_key
from venue_recommendation_mcp import TAG_FILE
from venue_search_intent import CALM_WORDS
from verify_tag_weather import normalize, read_tags


EXPLICIT_PUB_SUBTYPES = {
    "살얼음맥주/가성비호프", "경성레트로펍/파스타맥주", "야키토리/이자카야", "매운닭발/정통포차",
    "수제맥주펍", "캐주얼와인바", "포장마차", "오션뷰펍", "숙성생맥주펍", "살얼음생맥주/포차",
    "레트로감성펍", "몰트위스키바", "건어물포차",
}
VENUE_FIELDS = {
    "venue_id", "매장명", "업종구분", "세부카테고리", "자치구", "행정동", "행정동코드",
    "기상청격자_X", "기상청격자_Y", "도로명주소", "위도_lat", "경도_lng", "영업상태_코드",
    "매장분위기", "1인_평균예산(원)",
}
MENU_FIELDS = {
    "menu_id", "venue_id", "매장명", "자치구", "행정동", "메뉴판_카테고리", "메뉴명",
    "가격(원)", "메뉴설명_및_특징",
}
MENU_MARKER = " (매장 메뉴판 미확인)"


def source_pairs(source_dir, areas=None):
    available = {path.name.removesuffix("_venues.csv"): path for path in source_dir.glob("*_venues.csv")}
    names = sorted(set(areas)) if areas else sorted(available)
    if not names:
        raise ValueError("*_venues.csv와 *_menus.csv 파일 쌍이 필요합니다.")
    pairs = []
    for name in names:
        venue_path = available.get(name)
        menu_path = source_dir / f"{name}_menus.csv"
        if venue_path is None or not menu_path.is_file():
            raise ValueError(f"매장·메뉴 파일 쌍을 확인해 주세요: {name}")
        pairs.append((name, venue_path, menu_path))
    return pairs


def read_export(path, required):
    content = path.read_bytes()
    with StringIO(content.decode("utf-8-sig"), newline="") as source:
        reader = csv.DictReader(source)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)):
            raise ValueError(f"{path.name}에 중복 헤더가 있습니다.")
        missing = required.difference(headers)
        if missing:
            raise ValueError(f"{path.name} 누락 헤더 {sorted(missing)}")
        rows = []
        for row in reader:
            if None in row or any(row.get(field) is None for field in required):
                raise ValueError(f"{path.name}의 CSV 열 수를 확인해 주세요.")
            rows.append({key: (value or "").strip() for key, value in row.items()})
    return rows, {"file": path.name, "rows": len(rows), "sha256": hashlib.sha256(content).hexdigest()}


def exact_won(value):
    text = value.strip()
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", text):
        return None
    amount = int(text.replace(",", ""))
    return amount if amount > 0 else None


def coordinates(row):
    try:
        lat, lng = float(row["위도_lat"]), float(row["경도_lng"])
    except (ValueError, TypeError):
        return None
    if isfinite(lat) and isfinite(lng) and 36 <= lat <= 39 and 125 <= lng <= 129:
        return [lat, lng]
    return None


def menu_classifier(labels, ancestors):
    aliases = {
        alias: tag for alias, tag in labels["MENU_TYPE"].items()
        if alias != tag and len(alias) >= 2
    }
    aliases.update({"김치전": "menu_jeon", "부추전": "menu_jeon", "바닐라빈라떼": "menu_cafe_latte"})
    ordered = sorted(aliases, key=lambda value: (-len(value), value))

    def classify(name):
        tags = set()
        for part in re.split(r"\s+[&+]\s+", name):
            part = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", part)
            part = re.sub(r"\s*\([^()]*\)\s*$", "", part)
            part = re.sub(r"\s*\d+(?:\.\d+)?\s*(?:ml|cc|oz|g|잔|개|종|병)\s*$", "", part, flags=re.I)
            word = normalize(part)
            if word.startswith("파니니"):
                tags.add("menu_sandwich")
                continue
            for alias in ordered:
                if word == alias or (not alias.isascii() and word.endswith(alias)):
                    tag = aliases[alias]
                    if "menu_alcohol" in ancestors[tag] and any(
                        term in word for term in ("논알콜", "논알코올", "무알콜", "무알코올", "nonalcohol")
                    ):
                        break
                    tags.add(tag)
                    break
        return sorted(tag for tag in tags if not any(tag in ancestors[other] - {other} for other in tags))

    assert classify("김치전골") == ["menu_jeongol"]
    assert classify("김치전") == ["menu_jeon"]
    assert classify("파니니 멕시칸 치킨") == ["menu_sandwich"]
    assert classify("칼칼 김치 어묵 우동 나베") == ["menu_nabe"]
    assert "menu_chicken" in ancestors[classify("크리스피 순살 치킨")[0]]
    return classify


def prepare(source_dir, areas=None):
    place_index = SeoulPlaceIndex(capital=True)
    _, labels, ancestors = read_tags(TAG_FILE)
    classify_menu = menu_classifier(labels, ancestors)
    mood_labels = {**labels["MOOD"], **{word: "mood_calm" for word in CALM_WORDS}}
    venues, menus, sources = [], [], []
    audit = {}
    all_signatures = set()
    seen_venues, seen_menus = set(), set()
    pub_subtypes = CONFIRMED_PUB_SUBTYPES | EXPLICIT_PUB_SUBTYPES
    for district, venue_path, menu_path in source_pairs(source_dir, areas):
        raw_venues, source = read_export(venue_path, VENUE_FIELDS)
        sources.append(source)
        raw_menus, source = read_export(menu_path, MENU_FIELDS)
        sources.append(source)
        totals = Counter()
        excluded_venues, excluded_menus = Counter(), Counter()
        reasons_by_venue = {}
        source_venues = {row["venue_id"]: row for row in raw_venues}
        if len(source_venues) != len(raw_venues):
            raise ValueError(f"매장 ID가 중복됩니다: {venue_path.name}")
        groups = defaultdict(list)
        for row in raw_menus:
            groups[row["venue_id"]].append(row)
        signatures = {
            key: tuple(sorted((row["메뉴명"], row["가격(원)"], row["메뉴판_카테고리"], row["메뉴설명_및_특징"])
                              for row in group))
            for key, group in groups.items()
        }
        repetition = Counter(signatures.values())
        all_signatures.update(repetition)
        tagged_names = {}

        for raw in raw_venues:
            venue_id = raw["venue_id"].strip()
            if not venue_id or venue_id in seen_venues:
                raise ValueError(f"빈 매장 ID 또는 중복 ID가 있습니다. {district}")
            seen_venues.add(venue_id)
            region_id = f"region:{raw['행정동코드'].strip()}"
            region = place_index.weather_regions.get(region_id)
            reason = None
            address = raw["도로명주소"].strip()
            address_province = PROVINCE_ALIASES.get(address.split()[0]) if address else None
            if region is None or region["sido"] not in PROVINCE_LABELS:
                reason = "unsupported_region_code"
            elif (
                (address_province is not None and address_province != region["sido"])
                or not district_contains(region["sigungu"], raw["자치구"])
                or dong_key(region["name"]) != dong_key(raw["행정동"])
            ):
                reason = "region_code_district_conflict"
            elif raw["영업상태_코드"] not in {"OPEN", "1"}:
                reason = "source_status_excluded"
            elif not raw["매장명"].strip() or not raw["도로명주소"].strip():
                reason = "missing_name_or_address"
            subtype = normalize(raw["세부카테고리"])
            category = raw["업종구분"].strip()
            venue_category = "CAFE" if category == "카페" else (
                "PUB" if category == "술집" and subtype in pub_subtypes else None
            )
            if reason is None and venue_category is None:
                reason = "ambiguous_or_unsupported_venue_category"
            if reason:
                excluded_venues[reason] += 1
                reasons_by_venue[venue_id] = reason
                continue

            grid = [region["nx"], region["ny"]]
            if [raw["기상청격자_X"], raw["기상청격자_Y"]] != list(map(str, grid)):
                totals["source_grid_differs_used_official_grid"] += 1
            mood_ids = sorted({
                mood_labels[key] for word in re.split(r"[|;,]", raw["매장분위기"])
                if (key := normalize(word)) in mood_labels
            })
            budget = exact_won(raw["1인_평균예산(원)"])
            point = coordinates(raw)
            if point is None:
                totals["no_source_coordinates"] += 1
            if budget is None:
                totals["no_source_average_budget"] += 1
            venues.append({
                "venue_id": venue_id, "name": raw["매장명"].strip(), "address": raw["도로명주소"].strip(),
                "venue_category": venue_category, "sub_category": raw["세부카테고리"].strip() or None,
                "region_id": region_id, "province": region["sido"], "district": region["sigungu"],
                "admin_dong": region["name"], "weather_grid": grid,
                "coordinates": point, "mood_tag_ids": mood_ids, "avg_budget_krw": budget,
                "is_public": True, "source_business_status": raw["영업상태_코드"],
                "source_publication_status": None, "source_file": venue_path.name,
            })
            totals["venues_retained"] += 1
            totals[f"venue_{venue_category.lower()}"] += 1

        for raw in raw_menus:
            menu_id, venue_id = raw["menu_id"].strip(), raw["venue_id"].strip()
            if not menu_id or menu_id in seen_menus:
                raise ValueError(f"빈 메뉴 ID 또는 중복 ID가 있습니다. {district}")
            seen_menus.add(menu_id)
            parent = source_venues.get(venue_id)
            if parent is None:
                excluded_menus["unlinked_venue_id"] += 1
                continue
            if reason := reasons_by_venue.get(venue_id):
                excluded_menus[reason] += 1
                continue
            if (normalize(raw["매장명"]) != normalize(parent["매장명"])
                    or normalize(raw["자치구"]) != normalize(parent["자치구"])
                    or dong_key(raw["행정동"]) != dong_key(parent["행정동"])):
                excluded_menus["venue_identity_conflict"] += 1
                continue
            original = raw["메뉴명"].strip()
            if not original:
                excluded_menus["missing_menu_name"] += 1
                continue
            if original not in tagged_names:
                tagged_names[original] = classify_menu(original)
            type_ids = tagged_names[original]
            totals["tagged_menus" if type_ids else "untagged_menus"] += 1
            repeated = repetition[signatures[venue_id]] > 1
            totals["menus_in_repeated_sets" if repeated else "menus_without_source_evidence"] += 1
            menus.append({
                "menu_id": menu_id, "venue_id": venue_id, "name": original + MENU_MARKER,
                "source_menu_name": original, "price_krw": exact_won(raw["가격(원)"]),
                "menu_type_tag_ids": type_ids, "cuisine_tag_ids": [], "is_public": True,
                "source_assessment": "REPEATED_MENU_SET_UNVERIFIED" if repeated else "CSV_MENU_UNVERIFIED",
            })
            totals["menus_retained"] += 1

        assert totals["venues_retained"] + sum(excluded_venues.values()) == len(raw_venues)
        assert totals["menus_retained"] + sum(excluded_menus.values()) == len(raw_menus)
        audit[district] = {
            "source_venues": len(raw_venues), "source_menus": len(raw_menus),
            "source_business_statuses": dict(Counter(row["영업상태_코드"] for row in raw_venues)),
            "distinct_source_menu_sets": len(repetition),
            "source_menus_in_repeated_sets": sum(len(group) for key, group in groups.items()
                                                 if repetition[signatures[key]] > 1),
            **totals, "excluded_venues": dict(excluded_venues), "excluded_menus": dict(excluded_menus),
        }
    fingerprint = hashlib.sha256("".join(row["sha256"] for row in sources).encode()).hexdigest()[:12]
    return {
        "version": f"drive-preview-{fingerprint}", "preview_only": True,
        "source_basis": "팀 Drive CSV의 매장·메뉴 값. 반복 메뉴의 매장별 메뉴판 출처 미확인.",
        "visibility_basis": "원본 상태 코드 OPEN 또는 1인 검증 적합 행의 로컬 노출. 점주 공개 설정과 현재 영업은 미확인.",
        "budget_basis": "원본 1인_평균예산(원)과 가격(원)의 입력값. 추정값 추가나 실제 결제액 확인 없이 사용.",
        "tag_basis": "메뉴명과 원본 분위기 텍스트를 기존 공통 태그에 매핑. 이름으로 요리 계열을 부여하지 않음.",
        "weather_mapping_version": place_index.weather_mapping_version, "sources": sources, "audit": audit,
        "distinct_source_menu_sets_across_all_files": len(all_signatures), "independently_verified_menus": 0,
        "venues": venues, "menus": menus,
    }


def write_snapshot(data, output, *, replace=False):
    output = output.resolve()
    if output.suffix.lower() != '.json':
        raise ValueError('출력 파일은 .json으로 지정해 주세요.')
    if output.exists() and not replace:
        raise ValueError('출력 파일이 이미 있습니다. 갱신하려면 --replace를 지정해 주세요.')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                prefix='.venue-preview-', suffix='.json', delete=False) as target:
            temporary = Path(target.name)
            json.dump(data, target, ensure_ascii=False, separators=(',', ':'))
            target.write('\n')
        temporary.replace(output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir', type=Path, required=True, help='팀의 *_venues.csv와 *_menus.csv 폴더')
    parser.add_argument('--areas', nargs='+', help='파일 이름의 접두어. 생략하면 폴더 안의 모든 파일 쌍')
    parser.add_argument('--output', type=Path, required=True, help='로컬 검증용 JSON 경로')
    parser.add_argument('--replace', action='store_true', help='기존 출력 스냅샷을 검증된 변환 결과로 교체')
    args = parser.parse_args()
    try:
        data = prepare(args.source_dir, args.areas)
        write_snapshot(data, args.output, replace=args.replace)
    except (ValueError, OSError, csv.Error) as exc:
        parser.error(str(exc))
    print(json.dumps({
        'version': data['version'], 'preview_only': True,
        'venues': len(data['venues']), 'menus': len(data['menus']),
        'independently_verified_menus': data['independently_verified_menus'],
        'source_basis': data['source_basis'], 'audit': data['audit'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
