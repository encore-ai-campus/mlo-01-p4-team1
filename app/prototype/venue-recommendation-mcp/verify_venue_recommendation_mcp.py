"""공식 SDK stdio와 프로젝트 예보 HTTP 계약을 통한 실제 도구 호출 검증."""

import argparse
import asyncio
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
import csv
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx
import venue_forecast

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from venue_csv_catalog import add_csv_venues
from venue_forecast import KST, timestamp
from verify_tag_weather import read_tags


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "venue_recommendation_mcp.py"
SAMPLE_DATA = ROOT.parent / "app" / "backend" / "venues" / "sample_data"
if not SAMPLE_DATA.is_dir():
    SAMPLE_DATA = ROOT.parents[1] / "backend" / "venues" / "sample_data"
CATALOG = json.loads((ROOT / "실매장_태그_날씨_시범_v0.2.json").read_text(encoding="utf-8"))
LOCATIONS = json.loads((ROOT / "venue_weather_regions_v0.2.json").read_text(encoding="utf-8"))["locations"]


def output(visit, **kwargs):
    return {
        "region": "서울", "visit_at": visit.isoformat(), "orders": [{"types": ["menu_coffee"]}],
        **kwargs,
    }


@asynccontextmanager
async def session_for(*args):
    env = {
        name: os.environ[name] for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        ) if name in os.environ
    }
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", VENUE_WEATHER_BASE_URL="")
    if "--weather-base-url" in args:
        env.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    params = StdioServerParameters(command=sys.executable, args=["-B", str(SERVER), *args], env=env)
    async with asyncio.timeout(75):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "venue-recommendation"
                yield client


async def call(client, name, arguments):
    response = await client.call_tool(name, arguments=arguments)
    assert not response.isError and response.structuredContent is not None, response.content
    return response.structuredContent


def segment(start, issued, pty):
    return {
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "issued_at": issued.isoformat(), "received_at": (issued + timedelta(minutes=1)).isoformat(),
        "status": "valid", "pty": pty, "sky": 1 if pty == 0 else None,
    }


@contextmanager
def project_fixture(locations=LOCATIONS):
    now = datetime.now(KST).replace(microsecond=0)
    first = now.replace(minute=0, second=0) + timedelta(hours=1)
    issued = now - timedelta(minutes=5)
    by_region = {row["region_id"]: row for row in locations.values()}
    calls = []

    class WeatherHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path)
            region_id = parse_qs(path.query).get("region_id", [""])[0]
            place = by_region.get(region_id)
            if path.path != "/api/weather" or place is None:
                self.send_error(404)
                return
            calls.append(region_id)
            intervals = [
                segment(first, issued, 0),
                segment(first + timedelta(hours=1), issued, 1),
                segment(first + timedelta(hours=2), issued, 1 if place["nx"] == 57 else 0),
                segment(first + timedelta(hours=3), issued - timedelta(hours=3), 0),
            ]
            response = json.dumps({
                "is_test": False, "region_id": region_id, "nx": place["nx"], "ny": place["ny"],
                "weather_code": "RAIN", "recommendation_ready": False, "segments": intervals,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), WeatherHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", first, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def v02_projection(result):
    return {
        bucket: [{
            "venue_id": row["venue_id"],
            "matched_mood_ids": [mood["id"] for mood in row["matched_moods"]],
            "matched_menu_groups": [[menu["menu_id"] for menu in group] for group in row["matched_orders"]],
            "seat": row["seat"]["code"],
        } for row in result[bucket]]
        for bucket in ("ranked", "pending", "excluded")
    }


def print_case(name, result):
    decisions = {
        bucket: [(row["name"], row["seat"]["code"], row["forecast"]["state"])
                 for row in result[bucket]]
        for bucket in ("ranked", "pending", "excluded") if result[bucket]
    }
    print(f"PASS {name} → {result['status']} / {result['forecast_status']} / {decisions}")


async def verify_project():
    with project_fixture() as (url, first, requests):
        async with session_for("--weather-base-url", url) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"list_venue_tags", "recommend_venues"}
            assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools.tools)
            assert "weather" not in next(t for t in tools.tools if t.name == "recommend_venues").inputSchema["properties"]
            moods = await call(client, "list_venue_tags", {"kind": "MOOD"})
            assert len(moods["tags"]) == 24 and all(tag["kind"] == "MOOD" for tag in moods["tags"])
            assert "발산1동" in moods["regions"]
            print("PASS initialize · tools/list · 분위기 태그 24개 조회")

            visits = {
                "CLEAR": first + timedelta(minutes=30),
                "RAIN": first + timedelta(hours=1, minutes=30),
                "NO_FORECAST": first + timedelta(hours=12, minutes=30),
            }
            for case in CATALOG["cases"]:
                query = case["query"]
                arguments = {
                    "region": query["district"] or "서울", "visit_at": visits[query["weather"]].isoformat(),
                    "outdoor": query["seating"], "preferred_moods": query["preferred_moods"],
                    "orders": query["orders"], "limit": 20,
                }
                result = await call(client, "recommend_venues", arguments)
                assert v02_projection(result) == case["expected"], (case["name"], result)
                assert result["tag_version"] == result["catalog_version"] == "0.2"
                if query["weather"] == "NO_FORECAST":
                    for bucket in ("ranked", "pending", "excluded"):
                        assert all(row["forecast"]["reason"] == "TIME_NOT_COVERED" for row in result[bucket])
                print_case(case["name"], result)

            before = len(requests)
            mixed = await call(client, "recommend_venues", output(
                first + timedelta(hours=2), outdoor="REQUIRE_OUTDOOR",
            ))
            assert [row["forecast"]["grid"] for row in mixed["ranked"]] == [[61, 126]], mixed
            assert [row["forecast"]["grid"] for row in mixed["excluded"]] == [[57, 126]], mixed
            assert mixed["ranked"][0]["forecast"]["start_at"] == (first + timedelta(hours=2)).isoformat()
            assert len(requests) - before == 2, requests[before:]
            print_case("같은 방문 시각 · 두 격자 · 예보 구간 경계", mixed)

            stale = await call(client, "recommend_venues", output(
                first + timedelta(hours=3, minutes=10), region="발산1동", outdoor="REQUIRE_OUTDOOR",
            ))
            assert stale["ranked"][0]["forecast"]["reason"] == "STALE_OR_FUTURE_SEGMENT"
            assert stale["ranked"][0]["seat"]["code"] == "OUTDOOR_WEATHER_UNKNOWN"
            print_case("만료 예보", stale)

            before = len(requests)
            empty_args = output(visits["CLEAR"], region="강서구", orders=[
                {"cuisine": "cuisine_korean", "types": ["menu_cake"]},
            ])
            empty = await call(client, "recommend_venues", empty_args)
            assert empty["status"] == "NO_MATCH" and empty["forecast_status"] == "NOT_NEEDED"
            assert len(requests) == before, requests[before:]
            print("PASS 같은 메뉴 조건의 빈 결과 / 날씨 호출 0건")
            print("MCP_PROOF " + json.dumps({"arguments": empty_args, "response": empty}, ensure_ascii=False))

            invalid = (
                output(visits["CLEAR"], region="강남"),
                output(visits["CLEAR"], orders=[{}]),
                output(visits["CLEAR"], orders=[{"types": ["mood_retro"]}]),
                output(visits["CLEAR"], outdoor="OPEN"),
                {**output(visits["CLEAR"]), "visit_at": "2030-11-10T14:30:00"},
            )
            for arguments in invalid:
                response = await client.call_tool("recommend_venues", arguments=arguments)
                assert response.isError, response.content
                if arguments["region"] == "강남":
                    proof = {"arguments": arguments, "is_error": response.isError,
                             "content": [item.model_dump(mode="json") for item in response.content]}
                    print("MCP_PROOF " + json.dumps(proof, ensure_ascii=False))
            assert len(requests) == before, requests[before:]
            print(f"PASS 잘못된 입력 {len(invalid)}개 거절 / 날씨 호출 0건")
            print("검증용 HTTP GET 지역별 호출 수", dict(Counter(requests)))

    async with session_for() as client:
        no_provider = await call(client, "recommend_venues", output(
            visits["CLEAR"], region="강서구", outdoor="REQUIRE_OUTDOOR",
        ))
        assert no_provider["forecast_status"] == "NO_FORECAST", no_provider
        assert no_provider["ranked"][0]["forecast"]["reason"] == "PROVIDER_NOT_CONFIGURED"
        assert no_provider["ranked"][0]["seat"]["code"] == "OUTDOOR_WEATHER_UNKNOWN"
        print_case("기존 예보 API 미설정", no_provider)


async def verify_csv_sample():
    sample_data = SAMPLE_DATA
    catalog_path = ROOT / "fixtures" / "csv_sample_catalog.json"
    locations_path = ROOT / "fixtures" / "csv_sample_locations.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    locations = json.loads(locations_path.read_text(encoding="utf-8"))["locations"]
    venue = next(venue for venue in catalog["venues"] if venue["venue_id"] == "VN_SL_033264")
    venue_id = venue["venue_id"]

    with (sample_data / "seoul_venues_demo_sample.csv").open(encoding="utf-8-sig", newline="") as source:
        assert any(row["venue_id"] == venue_id for row in csv.DictReader(source))
    with (sample_data / "seoul_venue_menus_demo_sample.csv").open(encoding="utf-8-sig", newline="") as source:
        original_menus = {
            row["menu_id"]: row["메뉴명"] for row in csv.DictReader(source) if row["venue_id"] == venue_id
        }
    assert all(original_menus[item["menu_id"]] == item["name"]
               for item in catalog["menus"] if item["venue_id"] == venue_id)

    with project_fixture(locations) as (url, first, requests):
        async with session_for("--catalog", str(catalog_path), "--locations", str(locations_path),
                               "--weather-base-url", url) as client:
            for mode in ("REQUIRE_OUTDOOR", "PREFER_OUTDOOR"):
                args = output(
                    first + timedelta(minutes=30), region=venue["admin_dong"], outdoor=mode,
                    orders=[{"types": ["menu_coffee"]}, {"types": ["menu_cookie"]}],
                )
                result = await call(client, "recommend_venues", args)
                assert result["status"] == "OK" and result["forecast_status"] == "AVAILABLE", result
                matched, = result["ranked"]
                assert matched["venue_id"] == venue_id and matched["seat"]["code"] == "OUTDOOR", matched
                menu_groups = [{menu["menu_id"] for menu in group} for group in matched["matched_orders"]]
                assert menu_groups == [{"MN_DEMO_03581", "MN_DEMO_03582"}, {"MN_DEMO_03579"}], matched
                print_case(f"팀 시연 CSV 원본 ID · {mode}", result)
        assert requests == [locations[venue_id]["region_id"]] * 2, requests


async def verify_concierge_ranking():
    sample_path = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    visit = (datetime.now(KST) + timedelta(hours=1)).isoformat()
    arguments = {"query": "인당 3만원 정도로 잔잔한 술집", "region": "강남구", "visit_at": visit, "limit": 5}
    async with session_for("--venues-csv", str(sample_path)) as client:
        empty = await call(client, "recommend_venues", arguments)
        assert empty["status"] == "NO_MATCH" and empty["forecast_status"] == "NOT_NEEDED", empty
        assert empty["request"]["venue_category"] == "PUB" and empty["request"]["required_moods"] == ["mood_calm"]
        assert empty["request"]["budget"] == {"min_krw": 30000, "max_krw": 30000}
        pubs = await call(client, "recommend_venues", {**arguments, "query": "술집"})
        assert "VN_SL_000153" not in {row["venue_id"] for row in pubs["ranked"]}, pubs
        cafes = await call(client, "recommend_venues", {**arguments, "query": "1만원 미만 카공하기 좋은 카페"})
        assert len(cafes["ranked"]) == 5 and all(
            row["venue_category"] == "CAFE" and row["avg_budget_krw"] == 8000 for row in cafes["ranked"]
        ), cafes
        assert cafes["request"]["budget"] == {"min_krw": None, "max_krw": 9999}, cafes
        print("PASS 원본 강남구 CSV · 잔잔한 술집 근거 없음 · 샤브리스 제외")
        print("PASS 원본 강남구 CSV · 1만원 미만 카공 카페 5곳")

    with sample_path.open(encoding="utf-8-sig", newline="") as source:
        headers = csv.DictReader(source).fieldnames
    with TemporaryDirectory() as directory:
        test_path = Path(directory) / "concierge-test-venues.csv"
        with test_path.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=headers)
            writer.writeheader()
            for venue_id, category, subtype, mood, budget, status in (
                ("test_a", "술집", "생맥주 전문", "차분한|아늑한", 38000, "OPEN"),
                ("test_b", "술집", "와인바", "차분한", 29000, "OPEN"),
                ("test_c", "술집", "칵테일바", "조용한", "", "OPEN"),
                ("test_loud", "술집", "생맥주 전문", "활기찬", 30000, "OPEN"),
                ("test_closed", "술집", "와인바", "차분한", 30000, "CLOSED_HIDDEN"),
                ("test_cafe", "카페", "카페", "차분한", 30000, "OPEN"),
                ("test_unverified", "술집", "요리 주점", "차분한", 30000, "OPEN"),
            ):
                writer.writerow({
                    "venue_id": venue_id, "매장명": venue_id, "자치구": "강남구", "행정동": "신사동",
                    "도로명주소": "테스트 주소", "업종구분": category, "세부카테고리": subtype,
                    "매장분위기": mood, "1인_평균예산(원)": budget, "영업상태_코드": status,
                })
        async with session_for("--venues-csv", str(test_path)) as client:
            result = await call(client, "recommend_venues", arguments)
            assert [row["venue_id"] for row in result["ranked"]] == ["test_b", "test_a", "test_c"], result
            assert [row["avg_budget_krw"] for row in result["ranked"]] == [29000, 38000, None], result
            preferred = await call(client, "recommend_venues", {**arguments, "preferred_moods": ["아늑한"], "limit": 2})
            assert [row["venue_id"] for row in preferred["ranked"]] == ["test_a", "test_b"], preferred
            assert preferred["counts"]["ranked"] == 3, preferred
            no_budget = await call(client, "recommend_venues", {**arguments, "query": "조용한 술집"})
            assert [row["venue_id"] for row in no_budget["ranked"]] == ["test_a", "test_b", "test_c"], no_budget
            band = await call(client, "recommend_venues", {**arguments, "query": "3만원대 술집"})
            assert [row["venue_id"] for row in band["ranked"]] == ["test_a", "test_loud"], band
            invalid = await client.call_tool("recommend_venues", arguments={**arguments, "query": "술집 말고 카페"})
            assert invalid.isError, invalid
            print("PASS 합성 대조군 · 업종과 필수 분위기 · 분위기 우선 · 예산순 · 가격 누락 · 상위 2곳 제한")


async def verify_search_location_and_budget():
    sample_path = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    visit = (datetime.now(KST) + timedelta(hours=1)).isoformat()
    base = {"region": "강남구", "visit_at": visit, "limit": 5}
    async with session_for("--venues-csv", str(sample_path)) as client:
        tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == "recommend_venues")
        assert {"region", "visit_at", "query"} <= tool.inputSchema["properties"].keys()
        assert {"region", "visit_at"}.isdisjoint(tool.inputSchema.get("required", ()))
        assert "주요 장소" in tool.inputSchema["properties"]["region"]["description"]
        options = await call(client, "list_venue_tags", {})
        assert {"강남역", "신림역", "불광역", "천호역"} <= set(options["regions"])

        for args in (
            {"query": "강남역 근처 카페"},
            {"query": "강남역"},
            {"region": "강남역", "venue_category": "CAFE"},
            {"region": "종로구", "query": "강남역에서 카공하기 좋은 카페"},
        ):
            station = await call(client, "recommend_venues", args)
            location = station["request"]["location"]
            assert station["request"]["region"] == location["label"] == "강남역", station
            assert location["kind"] == "STATION" and location["radius_m"] == 1000, station
            venue, = station["ranked"]
            assert venue["venue_id"] == "VN_SL_021087" and venue["district"] == "서초구", station
            assert 890 <= venue["distance_m"] <= 915, station
            assert 0 <= (datetime.now(KST) - timestamp(station["request"]["visit_at"])).total_seconds() < 10
        print("PASS MCP 선택 입력 · 검색어만 호출 · 역 이름만 호출 · 입력 위치 우선 · 현재 방문 시각")

        cafe = await call(client, "recommend_venues", {**base, "query": "강남역9번출구 카페"})
        assert cafe["request"]["location"]["label"] == "강남역 9번 출구", cafe
        assert cafe["request"]["location"]["radius_m"] == 1000, cafe
        venue, = cafe["ranked"]
        assert venue["venue_id"] == "VN_SL_021087" and venue["district"] == "서초구", cafe
        assert 920 <= venue["distance_m"] <= 940, cafe
        assert cafe["request"]["location"]["kind"] == "EXIT", cafe
        inferred_exit = await call(client, "recommend_venues", {
            "region": "강남역", "query": "9번 출구 근처 카페",
        })
        assert inferred_exit["request"]["location"] == cafe["request"]["location"], inferred_exit
        assert inferred_exit["ranked"][0]["distance_m"] == venue["distance_m"], inferred_exit
        print("PASS 직접 지정한 9번 출구 · 지역에서 역 추론 · 구 경계 통과 · 직선 거리 표시")

        for query, reason in (
            ("강남역 9번출구 잔잔한 술집", "NO_CATEGORY"),
            ("강남역 99번 출구 카페", "EXIT_UNSUPPORTED"),
            ("강남역 근처 잔잔한 술집", "NO_CATEGORY"),
            ("서울에없는역 1번 출구 카페", "STATION_UNSUPPORTED"),
            ("9번 출구 카페", "STATION_REQUIRED"),
            ("강남역 9번 출구 또는 10번 출구 카페", "MULTIPLE_LOCATIONS"),
            ("강남역 9번 출구 8천원 미만 카페", "NO_BUDGET"),
        ):
            result = await call(client, "recommend_venues", {**base, "query": query})
            assert result["status"] == "NO_MATCH" and result["empty_reason"] == reason, (query, result)
            assert result["forecast_status"] == "NOT_NEEDED", (query, result)
        print("PASS 빈 결과 7가지 분리 · 예보 조회 생략")

        for price, min_krw, max_krw in (
            ("8천원 이하", None, 8000),
            ("0.8만원 이하", None, 8000),
            ("8,000원 이하", None, 8000),
            ("7천원~9천원", 7000, 9000),
            ("8천원부터 1만 5천원까지", 8000, 15000),
        ):
            query = f"강남역 9번 출구 {price} 카페"
            result = await call(client, "recommend_venues", {**base, "query": query})
            assert result["request"]["budget"] == {"min_krw": min_krw, "max_krw": max_krw}, (query, result)
            assert [venue["venue_id"] for venue in result["ranked"]] == ["VN_SL_021087"], (query, result)
        print("PASS 원·천 원·소수 만 원·범위 표기 5개 · 8천 원 포함과 제외 경계")


async def verify_seoul_place_index():
    sample = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    catalog = ROOT / "fixtures" / "csv_sample_catalog.json"
    locations = ROOT / "fixtures" / "csv_sample_locations.json"
    async with session_for("--catalog", str(catalog), "--locations", str(locations), "--venues-csv", str(sample)) as client:
        for name, district, expected_name in (
            ("신림역", "관악구", "시즈"),
            ("불광역", "은평구", "카페인중독 은평점"),
            ("천호역", "송파구", "힐링타로카페"),
        ):
            result = await call(client, "recommend_venues", {"region": "강남구", "query": f"{name} 근처 카페"})
            area = result["request"]["location"]
            assert area["label"] == name and area["kind"] == "STATION", result
            assert result["ranked"][0]["name"] == expected_name and result["ranked"][0]["district"] == district, result
            assert all(row["distance_m"] <= 1000 for row in result["ranked"]), result
        print("PASS 신림·불광·천호역 · 동일 위치 처리로 서로 다른 매장 · 구 경계 통과")

        landmark = await call(client, "recommend_venues", {"query": "서촌 커피랑 쿠키 있는 카페"})
        assert landmark["request"]["location"]["kind"] == "LANDMARK", landmark
        assert [row["name"] for row in landmark["ranked"]] == ["파스텔서촌점"], landmark
        empty = await call(client, "recommend_venues", {"query": "성수동 카페거리 술집"})
        assert empty["request"]["location"]["label"] == "성수카페거리" and empty["request"]["venue_category"] == "PUB", empty
        assert empty["status"] == "OK" and all(row["venue_category"] == "PUB" for row in empty["ranked"]), empty
        print("PASS 서촌과 성수카페거리 위치 조회 · 장소의 '카페'를 업종 조건과 분리")

        same_station = await call(client, "recommend_venues", {"query": "신촌역 1만원 미만 카페"})
        assert same_station["status"] == "NEEDS_CHECK" and same_station["empty_reason"] == "AMBIGUOUS_LOCATION", same_station
        assert {row["query"] for row in same_station["location_candidates"]} == {
            "2호선 신촌역 1만원 미만 카페", "경의중앙선 신촌역 1만원 미만 카페",
        }, same_station
        picked = await call(client, "recommend_venues", {"query": same_station["location_candidates"][0]["query"]})
        assert picked["request"]["location"]["label"] == "2호선 신촌역", picked
        assert picked["request"]["budget"]["max_krw"] == 9999, picked

        same_dong = await call(client, "recommend_venues", {"query": "신사동 카페"})
        assert {row["label"] for row in same_dong["location_candidates"]} == {
            "강남구 신사동", "관악구 신사동", "은평구 신사동",
        }, same_dong
        chosen = await call(client, "recommend_venues", {"query": "강남구 신사동 카페"})
        assert chosen["request"]["location"]["kind"] == "DONG" and len(chosen["ranked"]) == 5, chosen
        missing = await call(client, "recommend_venues", {"query": "연남동 카페"})
        assert missing["request"]["location"]["label"] == "마포구 연남동", missing
        assert missing["empty_reason"] == "NO_REGISTERED_VENUES", missing
        print("PASS 동명이역과 동명이동 · 후보 선택 후 조건 유지 · 등록 매장 없는 동 분리")

        fallback = await call(client, "recommend_venues", {"query": "신림역 2번 출구 근처 카페"})
        assert fallback["request"]["location"]["unmatched_exit"] == 2 and fallback["ranked"], fallback
        broad = await call(client, "recommend_venues", {"query": "서울 전역 카페", "region": "강남구"})
        assert broad["request"]["location"]["kind"] == "CITY" and broad["counts"]["ranked"] > 5, broad
        print("PASS 출구 좌표 없는 경우 역 주변으로 검색 · 서울 전역 선택")


async def verify_curated_recommendations():
    catalog_path = ROOT / "fixtures" / "csv_sample_catalog.json"
    locations_path = ROOT / "fixtures" / "csv_sample_locations.json"
    venues_csv = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    locations = json.loads(locations_path.read_text(encoding="utf-8"))["locations"]
    with project_fixture(locations) as (url, first, _):
        async with session_for("--catalog", str(catalog_path), "--locations", str(locations_path),
                               "--venues-csv", str(venues_csv), "--weather-base-url", url) as client:
            async def search(query, at=first, **conditions):
                return await call(client, "recommend_venues", {"query": query, "visit_at": at.isoformat(), **conditions})

            rain = first + timedelta(hours=1)
            sunny = await search("성수카페거리 레트로 카페")
            rainy = await search("성수카페거리 레트로 카페", rain)
            assert sunny["ranked"][0]["name"] == "어니언 성수", sunny
            assert rainy["ranked"][0]["name"] == "성수 대림창고", rainy
            for result, condition in ((sunny, "CLEAR"), (rainy, "RAIN")):
                assert result["forecast_status"] == "AVAILABLE", result
                assert result["ranked"][0]["forecast"]["condition"] == condition, result
                assert result["ranked"][0]["weather_match"]["source"] == "SEAT_EVIDENCE", result
                assert all("mood_retro" in {mood["id"] for mood in venue["matched_moods"]}
                           for venue in result["ranked"]), result
            print("PASS 성수 레트로 카페 · 맑음 어니언 → 비 대림창고 · 같은 분위기 조건 유지")

            sunny = await search("을지로 카페", preferred_moods=["아늑한"])
            rainy = await search("을지로 카페", rain, preferred_moods=["아늑한"])
            assert sunny["ranked"][0]["name"] == "호랑이", sunny
            assert rainy["ranked"][0]["name"] == "콘티뉴이티 을지로", rainy
            outdoors = await search("을지로 카페", rain, outdoor="REQUIRE_OUTDOOR")
            assert outdoors["status"] == "NEEDS_CHECK" and not outdoors["ranked"], outdoors
            assert "호랑이" in {venue["name"] for venue in outdoors["pending"]}, outdoors
            print("PASS 을지로 카페 · 맑음 호랑이 → 비 콘티뉴이티 · 우천 야외석 미확인은 보류")

            for at, expected in ((first, sunny), (rain, rainy)):
                typed = await call(client, "recommend_venues", {
                    "region": "을지로", "venue_category": "CAFE", "preferred_moods": ["아늑한"],
                    "visit_at": at.isoformat(),
                })
                assert [row["venue_id"] for row in typed["ranked"]] == [
                    row["venue_id"] for row in expected["ranked"]
                ], typed
            print("PASS 자연어와 구조화 조건의 동일 날씨 순위")

            for query, name, amount in (
                ("성수카페거리 6500원 이하 레트로 카페", "성수 대림창고", 6500),
                ("성수카페거리 2만원 이하 아늑한 술집", "성수오뎅", 16900),
                ("을지로 9800원 이하 술집", "토리키조쿠 을지로3가점", 9800),
            ):
                result = await search(query)
                venue, = result["ranked"]
                example = venue["budget_example"]
                assert venue["name"] == name and example["amount_krw"] == amount, result
                assert sum(item["price_krw"] for item in example["items"]) == amount, result
                assert venue["avg_budget_krw"] is None and result["request"]["budget_mode"] == "REQUIRE", result

            for query in ("성수카페거리 6500원 미만 레트로 카페", "을지로 9800원 미만 술집"):
                excluded = await search(query)
                assert excluded["status"] == "NO_MATCH" and excluded["empty_reason"] == "NO_BUDGET", excluded
                assert excluded["forecast_status"] == "NOT_NEEDED", excluded

            typed = await call(client, "recommend_venues", {
                "region": "을지로", "venue_category": "PUB", "visit_at": first.isoformat(),
                "budget": {"max_krw": 9800}, "budget_mode": "REQUIRE",
            })
            assert [venue["name"] for venue in typed["ranked"]] == ["토리키조쿠 을지로3가점"], typed
            print("PASS 성수·을지로 1인 주문 예시 · 상한 포함과 미만 제외 · 구조화 예산 상한")


async def verify_typed_area_and_menu():
    sample_path = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    catalog_path = ROOT / "fixtures" / "csv_sample_catalog.json"
    locations_path = ROOT / "fixtures" / "csv_sample_locations.json"
    visit = (datetime.now(KST) + timedelta(hours=1)).isoformat()
    async with session_for("--catalog", str(catalog_path), "--locations", str(locations_path),
                           "--venues-csv", str(sample_path)) as client:
        base = {"region": "강남구", "visit_at": visit}
        cookie = await call(client, "recommend_venues", {**base, "query": "종로구에서 커피랑 쿠키 있는 카페"})
        assert cookie["request"]["region"] == "종로구", cookie
        venue, = cookie["ranked"]
        assert venue["venue_id"] == "VN_SL_033264", cookie
        assert {item["name"] for group in venue["matched_orders"] for item in group} == {
            "콜드브루", "아메리카노", "초코청크쿠키",
        }, cookie
        cake = await call(client, "recommend_venues", {**base, "query": "종로구 케이크 카페"})
        assert cake["status"] == "NO_MATCH" and cake["empty_reason"] == "NO_MENU", cake
        ambiguous = await call(client, "recommend_venues", {**base, "query": "강남구 종로구 카페"})
        assert ambiguous["empty_reason"] == "MULTIPLE_LOCATIONS", ambiguous
        print("PASS 입력 자치구 적용 · 커피·쿠키 메뉴 근거 · 없는 메뉴 제외 · 지역 중복 안내")


async def verify_partial_forecast_deadline():
    now = datetime.now(KST).replace(microsecond=0)
    visit = now.replace(minute=0, second=0) + timedelta(hours=1)
    issued = now - timedelta(minutes=5)
    locations = {
        "fast": {"region_id": "region:fast", "nx": 60, "ny": 126},
        "slow": {"region_id": "region:slow", "nx": 61, "ny": 126},
        "unverified": {"region_id": "region:unverified", "nx": 62, "ny": 126},
    }
    slow_cancelled = False

    async def weather_api(request):
        nonlocal slow_cancelled
        name = request.url.params["region_id"].removeprefix("region:")
        if name == "slow":
            try:
                await asyncio.sleep(30)
            finally:
                slow_cancelled = True
        payload = {**locations[name], "segments": [segment(visit, issued, 0)]}
        if name != "unverified":
            payload["is_test"] = False
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(weather_api))
    with patch.object(venue_forecast, "FORECAST_LOOKUP_BUDGET_SECONDS", 0.25), \
         patch.object(venue_forecast.httpx, "AsyncClient", return_value=client):
        result = await venue_forecast.fetch_forecasts(locations, visit, project_url="http://localhost/weather")
    assert slow_cancelled and result["slow"].reason == "FORECAST_DEADLINE", result
    assert result["fast"].condition == "CLEAR", result
    assert result["unverified"].reason == "SOURCE_NOT_VERIFIED", result
    print("PASS 느린 예보 조회 중단 · 확인된 예보 유지 · 출처 미확인 예보 제외")


async def verify_weather_ranking():
    sample_path = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    _, labels, _ = read_tags(ROOT / "공통_태그_목록_v0.2.csv")
    locations = dict(LOCATIONS)
    catalog = add_csv_venues(CATALOG, locations, sample_path, labels["MOOD"])
    by_id = {venue["venue_id"]: venue for venue in catalog["venues"]}
    with project_fixture(locations) as (url, first, _):
        async with session_for("--venues-csv", str(sample_path), "--weather-base-url", url) as client:
            for region, query in (
                ("강남구", "1만원 이하 카페"),
                ("성동구", "카페"),
                ("도봉구", "술집"),
                ("광진구", "술집"),
                ("용산구", "술집"),
                ("중구", "카페"),
            ):
                args = {"region": region, "query": query, "limit": 10}
                dry = await call(client, "recommend_venues", {**args, "visit_at": first.isoformat()})
                rain = await call(client, "recommend_venues", {
                    **args, "visit_at": (first + timedelta(hours=1)).isoformat(),
                })
                dry_row = dry["ranked"][0]
                rain_row = rain["ranked"][0]
                assert dry_row["venue_id"] != rain_row["venue_id"], (region, dry_row, rain_row)
                assert dry_row["forecast"]["condition"] == "CLEAR" and rain_row["forecast"]["condition"] == "RAIN"
                assert dry_row["weather_match"] == {"label": "맑은 날", "source": "DEMO_CSV"}, dry_row
                assert rain_row["weather_match"] == {"label": "비 오는 날", "source": "DEMO_CSV"}, rain_row
                assert "맑은날" in by_id[dry_row["venue_id"]]["demo_weather_tags"]
                assert "비오는날" in by_id[rain_row["venue_id"]]["demo_weather_tags"]
                assert {row["venue_id"] for row in dry["ranked"]} == {row["venue_id"] for row in rain["ranked"]}
                print(f"PASS {region} {query} · 맑음 {dry_row['name']} → 비 {rain_row['name']}")

            missing = await call(client, "recommend_venues", {
                "region": "강남구", "query": "카페", "limit": 10,
                "visit_at": (first + timedelta(hours=12)).isoformat(),
            })
            assert missing["forecast_status"] == "NO_FORECAST" and missing["ranked"], missing
            assert all("weather_match" not in row for row in missing["ranked"]), missing
            print("PASS 예보 없을 때 날씨 가점과 근거 표시 생략")


async def verify_offline():
    await verify_project()
    await verify_csv_sample()
    await verify_concierge_ranking()
    await verify_search_location_and_budget()
    await verify_seoul_place_index()
    await verify_curated_recommendations()
    await verify_weather_ranking()
    await verify_typed_area_and_menu()
    await verify_partial_forecast_deadline()


async def verify_live(dry, rain, missing):
    async with session_for("--demo-open-meteo") as client:
        for visit, expectation in ((dry, "DRY"), (rain, "RAIN"), (missing, "NO_FORECAST")):
            for mode in ("REQUIRE_OUTDOOR", "PREFER_OUTDOOR"):
                args = output(visit, region="발산1동", outdoor=mode)
                result = await call(client, "recommend_venues", args)
                rows = result["ranked"] + result["pending"] + result["excluded"]
                assert len(rows) == 1, result
                forecast = rows[0]["forecast"]
                assert forecast["provider"] == "open_meteo", result
                assert forecast["state"] == expectation, (args, forecast)
                if expectation == "NO_FORECAST":
                    assert forecast["reason"] == "TIME_NOT_COVERED", forecast
                if expectation == "RAIN":
                    seat = "OUTDOOR_CLOSED_RAIN" if mode == "REQUIRE_OUTDOOR" else "INDOOR_FALLBACK"
                    assert rows[0]["seat"]["code"] == seat, result
                print_case(f"공개 예보 {visit.isoformat()} {mode}", result)
                print(json.dumps({"arguments": args, "response": result}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-at", type=timestamp)
    parser.add_argument("--rain-at", type=timestamp)
    parser.add_argument("--missing-at", type=timestamp)
    args = parser.parse_args()
    if args.live:
        if not all((args.dry_at, args.rain_at, args.missing_at)):
            parser.error("--live에는 강수 없음·비·예보 범위 밖 방문 시각 세 값이 필요합니다.")
        asyncio.run(verify_live(args.dry_at, args.rain_at, args.missing_at))
    else:
        asyncio.run(verify_offline())


if __name__ == "__main__":
    main()
