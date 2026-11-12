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
from urllib.parse import urlsplit


from venue_csv_catalog import CONFIRMED_PUB_SUBTYPES, is_out_of_scope_facility
from venue_nearby import PROVINCE_ALIASES, PROVINCE_LABELS, SeoulPlaceIndex, district_contains, dong_key
from venue_search_intent import CALM_WORDS
from verify_tag_weather import normalize, read_tags


TAG_FILE = Path(__file__).with_name("공통_태그_목록_v0.2.csv")
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
    """Read each requested folder once, keeping identically named areas distinct."""
    directories = [source_dir] if isinstance(source_dir, (str, Path)) else list(source_dir)
    if not directories:
        raise ValueError("*_venues.csv와 *_menus.csv 파일 쌍이 필요합니다.")
    resolved = [Path(directory).resolve() for directory in directories]
    if len(resolved) != len(set(resolved)):
        raise ValueError("같은 원본 폴더가 중복 지정되었습니다.")
    requested = set(areas or [])
    found = set()
    pending = []
    for directory in resolved:
        if not directory.is_dir():
            raise ValueError(f"원본 폴더를 찾을 수 없습니다: {directory}")
        available = sorted(directory.glob("*_venues.csv"))
        if not available:
            raise ValueError(f"매장·메뉴 파일 쌍이 없는 원본 폴더입니다: {directory}")
        for venue_path in available:
            name = venue_path.name.removesuffix("_venues.csv")
            if requested and name not in requested:
                continue
            found.add(name)
            menu_path = directory / f"{name}_menus.csv"
            if not menu_path.is_file():
                raise ValueError(f"매장·메뉴 파일 쌍을 확인해 주세요: {name}")
            pending.append((name, venue_path, menu_path))
    if missing := requested - found:
        raise ValueError(f"매장·메뉴 파일 쌍을 확인해 주세요: {', '.join(sorted(missing))}")
    if not pending:
        raise ValueError("*_venues.csv와 *_menus.csv 파일 쌍이 필요합니다.")
    counts = Counter(name for name, _, _ in pending)
    occurrences = Counter()
    pairs = []
    for name, venue_path, menu_path in pending:
        occurrences[name] += 1
        label = f"{name}#{occurrences[name]}" if counts[name] > 1 else name
        pairs.append((label, venue_path, menu_path))
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
    embedded_research = {}
    audit = {}
    all_signatures = set()
    seen_venues, seen_menus = set(), set()
    pub_subtypes = CONFIRMED_PUB_SUBTYPES | EXPLICIT_PUB_SUBTYPES
    for district, venue_path, menu_path in source_pairs(source_dir, areas):
        raw_venues, source = read_export(venue_path, VENUE_FIELDS)
        source['area'] = district
        sources.append(source)
        for raw in raw_venues:
            research_fields = ('매장분위기_조사', '매장특징_조사', '대표메뉴_조사', '좌석_편의_조사')
            if not raw.get('출처_URL', '').strip() or not any(raw.get(key, '').strip() for key in research_fields):
                continue
            vid = raw['venue_id']
            if not vid or vid in embedded_research:
                raise ValueError(f'내장 조사 자료에 빈 ID 또는 중복 ID가 있습니다: {vid}')
            embedded_research[vid] = ({
                'venue_id': vid, '매장명': raw['매장명'], '도로명주소': raw['도로명주소'],
                '매장분위기_조사': raw.get('매장분위기_조사', ''),
                '매장특징_조사': raw.get('매장특징_조사', ''),
                '대표메뉴_조사': raw.get('대표메뉴_조사', ''),
                '좌석_편의_조사': raw.get('좌석_편의_조사', ''),
                '출처_URL': raw['출처_URL'], '조사확인일': raw.get('조사확인일', ''),
                '제한사항': raw.get('제한사항', ''),
            }, 'update')
        raw_menus, source = read_export(menu_path, MENU_FIELDS)
        source['area'] = district
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
            if reason is None and is_out_of_scope_facility(raw["매장명"]):
                reason = "out_of_scope_facility"
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
    fingerprint_input = "".join(row["sha256"] for row in sources)
    if any(item["excluded_venues"].get("out_of_scope_facility") for item in audit.values()):
        fingerprint_input += "\nout_of_scope_facility"
    fingerprint = hashlib.sha256(fingerprint_input.encode()).hexdigest()[:12]
    snapshot = {
        "version": f"drive-preview-{fingerprint}", "preview_only": True,
        "source_basis": "팀 Drive CSV의 매장·메뉴 값. 반복 메뉴의 매장별 메뉴판 출처 미확인.",
        "visibility_basis": "원본 상태 코드 OPEN 또는 1인 검증 적합 행의 로컬 노출. 점주 공개 설정과 현재 영업은 미확인.",
        "budget_basis": "원본 1인_평균예산(원)과 가격(원)의 입력값. 추정값 추가나 실제 결제액 확인 없이 사용.",
        "tag_basis": "메뉴명과 원본 분위기 텍스트를 기존 공통 태그에 매핑. 이름으로 요리 계열을 부여하지 않음.",
        "weather_mapping_version": place_index.weather_mapping_version, "sources": sources, "audit": audit,
        "distinct_source_menu_sets_across_all_files": len(all_signatures), "independently_verified_menus": 0,
        "venues": venues, "menus": menus,
    }
    if embedded_research:
        _apply_research_records(snapshot, embedded_research, {})
    return snapshot


def _research_key(value):
    # Prefix spelling variants are common in the source exports; do not erase
    # digits, building numbers or unit information when comparing identities.
    text = (value or '').strip()
    for long_name, short_name in (('서울특별시', '서울'), ('인천광역시', '인천'), ('경기도', '경기')):
        text = text.replace(long_name, short_name)
    return re.sub(r'[\s;()（）]', '', text).casefold()


def _research_urls(records, *, home=False):
    urls = []
    has_visitor = False
    for row in records:
        if not isinstance(row, dict):
            continue
        url = (row.get('출처 URL' if home else '출처_URL') or '').strip()
        kind = (row.get('출처 유형' if home else '출처구분') or '').strip()
        identity = (row.get('같은 매장이라고 판단한 근거' if home else '매장일치근거') or '').strip()
        if not home and row.get('채택상태') != '보완안_근거':
            continue
        parsed = urlsplit(url)
        if parsed.scheme not in {'https', 'http'} or not parsed.netloc or not identity:
            continue
        if (home and '방문' in kind) or (not home and kind == 'VISITOR'):
            has_visitor = True
        if url not in urls:
            urls.append(url)
    return urls if has_visitor else []


# Match only explicit, positive phrases in the already-reviewed mood field.
# Reject a whole clause on negation or uncertainty rather than guessing scope.
_MOOD_UNCERTAIN = re.compile(r'아니|않|없|못|불가|미확인|불명|확인\s*필요|확인\s*안|보장|의심|모르|아쉬|시끄럽|불편|부족|혼잡|변동|달라질|~|[?？]')
_MOOD_PHRASES = (
    (r'아늑(?:한|하고|하며|함|해요|했)', 'mood_cozy'),
    (r'(?:차분(?:한|하고|하며|함|해요|했)|조용(?:한|하고|하며|함|해요|했))', 'mood_calm'),
    (r'여유로(?:운|우며|움|워요|웠)', 'mood_relaxed'),
    (r'활기(?:찬|차고|참|차며)', 'mood_lively'),
    (r'고즈넉(?:한|하고|하며|함|해요|했)', 'mood_tranquil'),
    (r'아담(?:한|하고|하며|함|해요|했)', 'mood_intimate'),
    (r'(?:레트로|빈티지|복고)(?:한|풍|\s*감성|\s*인테리어|\s*분위기)', 'mood_retro'),
    (r'(?:한옥(?:과|의|\s*카페|\s*공간|\s*분위기|\s*인테리어)|전통\s*한옥)', 'mood_hanok'),
    (r'(?:정원(?:과|이\s*(?:있는|어우러진)|\s*분위기|\s*감성)|마당과\s*정원)', 'mood_garden'),
    (r'(?:전시(?:와|가\s*있는|\s*공간)|갤러리(?:\s*카페|\s*공간|\s*분위기))', 'mood_gallery'),
    (r'(?:햇살이\s*드는|채광이\s*좋은|채광\s*좋은)', 'mood_daylight'),
    (r'넓고\s*쾌적(?:한|하고|함)', 'mood_spacious'),
)


def _reviewed_moods(text, mood_labels):
    found = set()
    for clause in re.split(r'[|;\n.!。]', text):
        if not clause.strip() or len(clause) > 100 or _MOOD_UNCERTAIN.search(clause):
            continue
        key = normalize(clause)
        if key in mood_labels:
            found.add(mood_labels[key])
        for pattern, tag in _MOOD_PHRASES:
            if re.search(pattern, clause):
                found.add(tag)
    return sorted(found)


def _reviewed_menu_names(text):
    """Split explicit lists without splitting prices or parenthetical names."""
    names, current, depth = [], [], 0
    for index, char in enumerate(text):
        if char in '([（':
            depth += 1
        elif char in ')]）':
            depth = max(0, depth - 1)
        comma_between_digits = (char == ',' and index > 0 and index + 1 < len(text)
                                and text[index - 1].isdigit() and text[index + 1].isdigit())
        if depth == 0 and (char == '|' or (char == ',' and not comma_between_digits)):
            names.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
    names.append(''.join(current).strip())
    return names


def apply_research(snapshot, local_path, source_path, home_path):
    """Add checked research without deleting the team catalog's values."""
    if snapshot.get('preview_only') is not True:
        raise ValueError('조사 자료는 로컬 시연용 스냅샷에서만 사용할 수 있습니다.')
    local_rows, _ = read_export(local_path, {'venue_id','매장명','도로명주소','매장분위기','매장소개_후기','대표메뉴_후기','조사상태','확인일','제한사항'})
    source_rows, _ = read_export(source_path, {'venue_id','출처구분','출처_URL','매장일치근거','채택상태'})
    home_rows, _ = read_export(home_path, {'venue_id','매장명','주소','분위기','매장 특징','확인된 메뉴','확인일','제한사항','출처목록','보류목록','검토사유'})
    source_by_id = defaultdict(list)
    for row in source_rows:
        source_by_id[row['venue_id']].append(row)
    records = {}
    for row in local_rows:
        vid = row['venue_id']
        if not vid or vid in records:
            raise ValueError(f'조사 자료에 빈 ID 또는 중복 ID가 있습니다: {vid}')
        records[vid] = (row, False)
    for row in home_rows:
        vid = row['venue_id']
        if not vid or vid in records:
            raise ValueError(f'조사 자료에 빈 ID 또는 중복 ID가 있습니다: {vid}')
        records[vid] = (row, True)

    return _apply_research_records(snapshot, records, source_by_id)


def apply_research_update(snapshot, update_path):
    """Use the consolidated, previously reviewed Drive CSV in a preview."""
    if snapshot.get('preview_only') is not True:
        raise ValueError('조사 자료는 로컬 시연용 스냅샷에서만 사용할 수 있습니다.')
    rows, _ = read_export(update_path, {'venue_id','매장명','도로명주소','매장분위기_조사',
                                        '매장특징_조사','대표메뉴_조사','출처_URL','조사확인일','제한사항'})
    records = {}
    for row in rows:
        vid = row['venue_id']
        if not vid or vid in records:
            raise ValueError(f'조사 자료에 빈 ID 또는 중복 ID가 있습니다: {vid}')
        records[vid] = (row, 'update')
    return _apply_research_records(snapshot, records, {})


def _apply_research_records(snapshot, records, source_by_id):

    _, labels, ancestors = read_tags(TAG_FILE)
    mood_labels = {**labels['MOOD'], **{word: 'mood_calm' for word in CALM_WORDS}}
    classify_menu = menu_classifier(labels, ancestors)
    audit = Counter()
    retained_menus = list(snapshot['menus'])
    existing_menu_ids = {m['menu_id'] for m in retained_menus}
    existing_menu_names = defaultdict(set)
    for menu in retained_menus:
        original = menu.get('source_menu_name') or menu.get('name', '').removesuffix(MENU_MARKER)
        if original:
            existing_menu_names[menu['venue_id']].add(normalize(original))
    for venue in snapshot['venues']:
        pair = records.get(venue['venue_id'])
        if pair is None:
            audit['no_research'] += 1
            continue
        if venue.get('research'):
            audit['already_enriched'] += 1
            continue
        row, home = pair
        update = home == 'update'
        name, address = row['매장명'], row['주소' if home is True else '도로명주소']
        if _research_key(name) != _research_key(venue['name']) or _research_key(address) != _research_key(venue['address']):
            audit['identity_conflict'] += 1
            continue
        if update:
            urls = []
            for raw_url in row['출처_URL'].split('|'):
                url = raw_url.strip()
                parsed = urlsplit(url)
                if parsed.scheme in {'https', 'http'} and parsed.netloc and url not in urls:
                    urls.append(url)
        elif home:
            try:
                home_sources = json.loads(row['출처목록'])
                holds = json.loads(row['보류목록'])
            except (ValueError, TypeError):
                audit['invalid_source_data'] += 1
                continue
            if not isinstance(home_sources, list) or not isinstance(holds, list) or holds or row['검토사유']:
                audit['held_or_invalid'] += 1
                continue
            urls = _research_urls(home_sources, home=True)
        else:
            if row['조사상태'] not in {'후기_단일출처', '후기_복수출처'}:
                audit['held_or_invalid'] += 1
                continue
            urls = _research_urls(source_by_id.get(venue['venue_id'], []))
        if not urls:
            audit['missing_visitor_source'] += 1
            continue
        mood = row['매장분위기_조사' if update else ('분위기' if home else '매장분위기')]
        verified_moods = _reviewed_moods(mood, mood_labels)
        if verified_moods:
            venue['mood_tag_ids'] = verified_moods
            audit['moods_replaced'] += 1
        description = row['매장특징_조사' if update else ('매장 특징' if home else '매장소개_후기')]
        seating = row.get('좌석_편의_조사' if update else ('좌석·편의정보' if home else '좌석_및_편의정보'), '')
        venue['research'] = {'description': description, 'sources': urls, 'seating': seating,
                             'limitations': row['제한사항'], 'checked_on': row['조사확인일' if update else '확인일']}
        menu_text = row['대표메뉴_조사' if update else ('확인된 메뉴' if home else '대표메뉴_후기')]
        seen_names = set()
        for part in _reviewed_menu_names(menu_text):
            name = part.strip()
            # Do not turn descriptive comments into menu names.
            if (not name or len(name) > 40 or name in seen_names or '\n' in name
                    or re.search(r'\d[\d,]*\s*원|미확인|확인\s*필요|불명|없음', name)):
                continue
            seen_names.add(name)
            name_key = normalize(name)
            if name_key in existing_menu_names[venue['venue_id']]:
                audit['research_menu_duplicate'] += 1
                continue
            digest = hashlib.sha256((venue['venue_id'] + '\0' + name).encode('utf-8')).hexdigest()[:20]
            menu_id = 'research-' + digest
            if menu_id in existing_menu_ids:
                raise ValueError(f'조사 메뉴 ID가 기존 메뉴와 충돌합니다: {menu_id}')
            existing_menu_ids.add(menu_id)
            existing_menu_names[venue['venue_id']].add(name_key)
            retained_menus.append({'menu_id': menu_id, 'venue_id': venue['venue_id'],
                                   'name': name, 'price_krw': None, 'menu_type_tag_ids': classify_menu(name),
                                   'cuisine_tag_ids': [], 'is_public': True})
            audit['historical_menu_names'] += 1
        audit['applied'] += 1
    snapshot['menus'] = retained_menus
    snapshot['research_audit'] = dict(audit)
    snapshot['source_basis'] = '팀 원본 매장·메뉴와 공개 방문 후기 조사 자료. 조사 근거는 매장별 research 참조.'
    snapshot['tag_basis'] = '팀 원본 태그 유지; 조사 분위기를 기존 태그에 매핑할 수 있는 경우에만 교체.'
    return snapshot


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
    parser.add_argument('--source-dir', type=Path, required=True, action='append',
                        help='팀의 *_venues.csv와 *_menus.csv 폴더. 여러 지역 폴더는 옵션을 반복 지정')
    parser.add_argument('--areas', nargs='+', help='파일 이름의 접두어. 생략하면 폴더 안의 모든 파일 쌍')
    parser.add_argument('--output', type=Path, required=True, help='로컬 검증용 JSON 경로')
    parser.add_argument('--replace', action='store_true', help='기존 출력 스냅샷을 검증된 변환 결과로 교체')
    parser.add_argument('--research-local', type=Path, help='노트북 조사 매장 보완 CSV')
    parser.add_argument('--research-sources', type=Path, help='노트북 조사 출처 CSV')
    parser.add_argument('--research-home', type=Path, help='집 조사 중 대조 통과한 연결 후보 CSV')
    parser.add_argument('--research-update', type=Path, help='대조 통과한 통합 매장조사 CSV')
    args = parser.parse_args()
    try:
        research_paths = (args.research_local, args.research_sources, args.research_home)
        if args.research_update and any(research_paths):
            raise ValueError('--research-update와 세 조사 CSV 옵션은 함께 사용할 수 없습니다.')
        if any(research_paths) and not all(research_paths):
            raise ValueError('조사 자료를 적용하려면 세 조사 CSV 경로를 모두 지정해 주세요.')
        data = prepare(args.source_dir, args.areas)
        if any(research_paths):
            data = apply_research(data, *research_paths)
        elif args.research_update:
            data = apply_research_update(data, args.research_update)
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
