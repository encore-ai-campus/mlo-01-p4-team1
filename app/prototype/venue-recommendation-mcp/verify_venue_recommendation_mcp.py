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

from venue_forecast import KST, timestamp


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
    return [{
        "venue_id": row["venue_id"],
        "matched_mood_ids": [mood["id"] for mood in row["matched_moods"]],
        "matched_menu_groups": [[menu["menu_id"] for menu in group] for group in row["matched_orders"]],
    } for row in result["ranked"]]


def print_case(name, result):
    ranked = [(row["name"], row["forecast"]["state"]) for row in result["ranked"]]
    print(f"PASS {name} → {result['status']} / {result['forecast_status']} / {ranked}")


async def verify_project():
    with project_fixture() as (url, first, requests):
        async with session_for("--weather-base-url", url) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {"list_venue_tags", "recommend_venues"}
            assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools.tools)
            recommendation = next(tool for tool in tools.tools if tool.name == "recommend_venues")
            assert not {"weather", "outdoor"} & recommendation.inputSchema["properties"].keys()
            moods = await call(client, "list_venue_tags", {"kind": "MOOD"})
            assert len(moods["tags"]) == 24 and all(tag["kind"] == "MOOD" for tag in moods["tags"])
            assert "발산1동" in moods["regions"]
            print("PASS initialize · tools/list · 분위기 태그 24개 조회")

            visits = {
                "CLEAR": first + timedelta(minutes=30),
                "NO_FORECAST": first + timedelta(hours=12, minutes=30),
            }
            for case in CATALOG["cases"]:
                query = case["query"]
                if query["seating"] != "ANY" or query["weather"] != "NO_FORECAST":
                    continue
                arguments = {
                    "region": query["district"] or "서울", "visit_at": visits["NO_FORECAST"].isoformat(),
                    "preferred_moods": query["preferred_moods"], "orders": query["orders"], "limit": 20,
                }
                result = await call(client, "recommend_venues", arguments)
                expected = [{key: value for key, value in row.items() if key != "seat"}
                            for row in case["expected"]["ranked"]]
                assert v02_projection(result) == expected, (case["name"], result)
                assert result["tag_version"] == result["catalog_version"] == "0.2"
                assert not result["pending"] and not result["excluded"]
                assert all(row["forecast"]["reason"] == "TIME_NOT_COVERED" for row in result["ranked"])
                print_case(case["name"], result)

            before = len(requests)
            boundary = first + timedelta(hours=2)
            mixed = await call(client, "recommend_venues", output(boundary))
            grids = {tuple(row["forecast"]["grid"]): row["forecast"]["state"] for row in mixed["ranked"]}
            assert grids == {(61, 126): "DRY", (57, 126): "RAIN"}, mixed
            assert all(row["forecast"]["start_at"] == boundary.isoformat() for row in mixed["ranked"])
            assert len(requests) - before == 2, requests[before:]
            print_case("같은 방문 시각 · 두 격자 · 예보 구간 경계", mixed)

            stale = await call(client, "recommend_venues", output(
                first + timedelta(hours=3, minutes=10), region="발산1동",
            ))
            assert stale["ranked"][0]["forecast"]["reason"] == "STALE_OR_FUTURE_SEGMENT"
            assert "weather_match" not in stale["ranked"][0]
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
                output(visits["CLEAR"], limit=0),
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
            visits["CLEAR"], region="강서구",
        ))
        assert no_provider["forecast_status"] == "NO_FORECAST", no_provider
        assert no_provider["ranked"][0]["forecast"]["reason"] == "PROVIDER_NOT_CONFIGURED"
        assert "weather_match" not in no_provider["ranked"][0]
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

    venue.pop("seats")
    with TemporaryDirectory() as directory, project_fixture(locations) as (url, first, requests):
        seatless_catalog = Path(directory) / "catalog.json"
        seatless_catalog.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        async with session_for("--catalog", str(seatless_catalog), "--locations", str(locations_path),
                               "--weather-base-url", url) as client:
            args = output(
                first + timedelta(minutes=30), region=venue["admin_dong"],
                orders=[{"types": ["menu_coffee"]}, {"types": ["menu_cookie"]}],
            )
            result = await call(client, "recommend_venues", args)
            assert result["status"] == "OK" and result["forecast_status"] == "AVAILABLE", result
            matched, = result["ranked"]
            assert matched["venue_id"] == venue_id and "seat" not in matched, matched
            menu_groups = [{menu["menu_id"] for menu in group} for group in matched["matched_orders"]]
            assert menu_groups == [{"MN_DEMO_03581", "MN_DEMO_03582"}, {"MN_DEMO_03579"}], matched
            print_case("좌석 데이터 없는 매장의 원본 ID와 메뉴 근거", result)
        assert requests == [locations[venue_id]["region_id"]], requests


async def verify_concierge_ranking():
    sample_path = SAMPLE_DATA / "seoul_venues_demo_sample.csv"
    visit = (datetime.now(KST) + timedelta(hours=1)).isoformat()
    arguments = {"query": "인당 3만원 정도로 잔잔한 술집", "region": "강남구", "visit_at": visit, "limit": 5}
    async with session_for("--venues-csv", str(sample_path)) as client:
        empty = await call(client, "recommend_venues", arguments)
        assert empty["status"] == "NO_MATCH" and empty["forecast_status"] == "NOT_NEEDED", empty
        assert empty["request"]["venue_category"] == "PUB" and empty["request"]["required_moods"] == ["mood_calm"]
        assert empty["request"]["budget"] == {"min_krw": 30000, "max_krw": 30000}
        structured = await call(client, "recommend_venues", {
            "region": "강남구", "venue_category": "CAFE", "preferred_moods": ["잔잔한"],
            "request_text": arguments["query"],
        })
        assert structured["status"] == "NO_MATCH" and structured["empty_reason"] == empty["empty_reason"], structured
        assert not structured["ranked"], structured
        assert structured["request"]["venue_category"] == "PUB", structured
        assert structured["request"]["required_moods"] == ["mood_calm"], structured
        assert structured["request"]["budget_mode"] == "PREFER", structured
        pubs = await call(client, "recommend_venues", {**arguments, "query": "술집"})
        assert "VN_SL_000153" not in {row["venue_id"] for row in pubs["ranked"]}, pubs
        cafes = await call(client, "recommend_venues", {**arguments, "query": "1만원 미만 카공하기 좋은 카페"})
        assert len(cafes["ranked"]) == 5 and all(
            row["venue_category"] == "CAFE" and row["avg_budget_krw"] < 10000 for row in cafes["ranked"]
        ), cafes
        waffle = next(row for row in cafes["ranked"] if row["venue_id"] == "VN_SL_000005")
        assert waffle["sub_category"] == "와플/벨기에디저트카페" and waffle["avg_budget_krw"] == 9500, waffle
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
            capped = await call(client, "recommend_venues", {
                "region": "강남구", "venue_category": "PUB", "preferred_moods": ["아늑한"],
                "request_text": "인당 3만원 이하 조용한 술집",
            })
            assert [row["venue_id"] for row in capped["ranked"]] == ["test_b"], capped
            assert capped["request"]["budget_mode"] == "REQUIRE", capped
            flexible = await call(client, "recommend_venues", {
                "region": "강남구", "venue_category": "PUB", "preferred_moods": ["조용한"],
                "request_text": "가능하면 잔잔한 술집으로 추천해 주세요.",
            })
            assert not flexible["request"]["required_moods"] and flexible["counts"]["ranked"] > 3, flexible
            no_budget = await call(client, "recommend_venues", {**arguments, "query": "조용한 술집"})
            assert [row["venue_id"] for row in no_budget["ranked"]] == ["test_a", "test_b", "test_c"], no_budget
            band = await call(client, "recommend_venues", {**arguments, "query": "3만원대 술집"})
            assert [row["venue_id"] for row in band["ranked"]] == ["test_a", "test_loud"], band
            exclusion = await call(client, "recommend_venues", {**arguments, "query": "술집 말고 카페"})
            assert exclusion["empty_reason"] == "CONDITION_UNVERIFIED" and not exclusion["ranked"], exclusion
            print("PASS 합성 대조군 · 업종과 필수 분위기 · 분위기 우선 · 예산순 · 가격 누락 · 상위 2곳 제한")


async def verify_unverified_intent():
    with project_fixture() as (url, first, requests):
        async with session_for("--weather-base-url", url) as client:
            typed = {"region": "마포구 서교동", "venue_category": "PUB", "visit_at": first.isoformat()}
            for extra in (
                {"query": "서교동 헌팅술집"},
                {"request_text": "서교동에서 처음 보는 사람들이랑 어울리기 좋은 술집"},
                {"request_text": "쿠키는 빼고 커피 카페"},
                {"request_text": "쿠키 없는 카페"},
                {"request_text": "커피 또는 쿠키 카페"},
                {"request_text": "1인 1만원 또는 2만원 술집"},
                {"unverified_conditions": [{"kind": "OTHER", "text": "수화 공연을 볼 수 있는 곳"}]},
            ):
                result = await call(client, "recommend_venues", {**typed, **extra})
                assert result["status"] == "NEEDS_CHECK" and result["empty_reason"] == "CONDITION_UNVERIFIED", result
                assert not result["ranked"] and result["forecast_status"] == "NOT_NEEDED", result
                assert result["unverified_conditions"][0]["evidence_status"] == "UNKNOWN", result
            assert not requests, requests
            coffee = await call(client, "recommend_venues", {
                "request_text": "강서구에서 친구랑 모임 전에 커피 한잔", "region": "강서구",
                "orders": [{"types": ["커피"]}], "visit_at": first.isoformat(),
            })
            assert coffee["status"] == "OK" and coffee["ranked"], coffee
            print("PASS 미확인 목적·제외·선택 조건·모호한 예산에는 카드 없이 확인 안내 · 모임 전 커피는 정상 추천")


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
            ("불광역", "은평구", "이디야커피"),
            ("천호역", "송파구", "힐링타로카페"),
        ):
            result = await call(client, "recommend_venues", {"region": "강남구", "query": f"{name} 근처 카페"})
            area = result["request"]["location"]
            assert area["label"] == name and area["kind"] == "STATION", result
            assert result["ranked"][0]["name"] == expected_name and result["ranked"][0]["district"] == district, result
            assert all(row["distance_m"] <= 1000 for row in result["ranked"]), result
            assert [row["distance_m"] for row in result["ranked"]] == sorted(row["distance_m"] for row in result["ranked"]), result
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
        outside_demo = await call(client, "recommend_venues", {"query": "인천 송도 카페"})
        assert outside_demo["empty_reason"] == "REGION_UNSUPPORTED" and not outside_demo["ranked"], outside_demo
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
            cases = (
                ("성수카페거리 레트로 카페", {},
                 ["할아버지공장", "성수 대림창고", "어니언 성수"],
                 ["성수 대림창고", "할아버지공장", "어니언 성수"]),
                ("을지로 카페", {"preferred_moods": ["아늑한"]},
                 ["콘티뉴이티 을지로", "리오네커피", "피즈소셜클럽"],
                 ["콘티뉴이티 을지로", "피즈소셜클럽", "호랑이"]),
                ("성수카페거리 술집", {},
                 ["서울브루어리 성수", "용용선생 마라도원", "성수오뎅"],
                 ["성수오뎅", "세스크 멘슬", "용용선생 마라도원"]),
                ("을지로 술집", {},
                 ["더랜치브루잉 을지로3가점", "니어민트+ 을지로", "토리키조쿠 을지로3가점"],
                 ["끽비어컴퍼니 을지로", "니어민트+ 을지로", "더랜치브루잉 을지로3가점"]),
            )
            compared = {}
            for query, conditions, clear_names, rain_names in cases:
                sunny = await search(query, first, **conditions)
                rainy = await search(query, rain, **conditions)
                compared[query] = sunny, rainy
                summary = {"query": query}
                for result, condition, names in ((sunny, "CLEAR", clear_names), (rainy, "RAIN", rain_names)):
                    assert result["status"] == "OK" and not result["pending"] and not result["excluded"], result
                    assert result["ranked"][0]["forecast"]["condition"] == condition, result
                    assert result["ranked"][0]["weather_match"]["source"] == "TAG_MATCH", result
                    assert [row["name"] for row in result["ranked"][:3]] == names, result
                    assert all("seat" not in row for row in result["ranked"]), result
                    summary[condition] = [
                        [row["name"], (row.get("weather_match") or {}).get("label")]
                        for row in result["ranked"][:3]
                    ]
                print("MCP_RANKING " + json.dumps(summary, ensure_ascii=False))

            for result in compared["성수카페거리 레트로 카페"]:
                assert all("mood_retro" in {mood["id"] for mood in venue["matched_moods"]}
                           for venue in result["ranked"]), result

            sunny, rainy = compared["성수카페거리 술집"]
            assert sunny["ranked"][0]["weather_match"]["label"] == "맑은 날 · 활기찬 · 서울 라이스 라거 400mL"
            assert rainy["ranked"][0]["weather_match"]["label"] == "비 오는 날 · 아늑한 · 홀리 세트 (모둠 오뎅, 2인)"
            beer = await search("성수카페거리 술집 생맥주", rain)
            odeng = next(row for row in beer["ranked"] if row["name"] == "성수오뎅")
            assert odeng["weather_match"]["label"] == "비 오는 날 · 아늑한", odeng
            print("PASS 검색에 지정한 메뉴의 근거로만 날씨 메뉴 가점 적용")

            missing = await search("을지로 카페", first + timedelta(hours=12))
            assert missing["forecast_status"] == "NO_FORECAST" and missing["ranked"], missing
            assert all("weather_match" not in row for row in missing["ranked"]), missing
            print("PASS 예보가 없으면 날씨 가점과 근거 생략")

            for at in (first, rain):
                craving = await search("성수카페거리 맥주 땡긴다", at)
                winner, fallback = craving["ranked"]
                assert winner["name"] == "서울브루어리 성수", craving
                assert winner["paired_menu"]["name"] == "서울브루어리 프라이드 치킨", craving
                assert fallback["name"] == "성수오뎅", craving
                assert fallback["paired_menu"]["name"] == "홀리 세트 (모둠 오뎅, 2인)", craving
                assert all(len(row["matched_orders"]) == 1 for row in craving["ranked"]), craving
            print("PASS 맥주가 당길 때 치킨 우선 · 치킨 없는 곳의 확인된 국물 안주는 후순위")

            pairing_cases = (
                ("맥주땡긴다", "서울브루어리 성수", "서울브루어리 프라이드 치킨"),
                ("을지로 생맥주 한 잔 하고 싶다", "토리키조쿠 을지로3가점", "닭다리살 키조쿠 타레 1주문(꼬치 2개)"),
                ("서촌에서 커피 한 잔", "파스텔서촌점", "초코청크쿠키"),
                ("마포구 비 오는 날 막걸리가 당겨", "산울림1992", "해물파전"),
            )
            for query, venue_name, paired_name in pairing_cases:
                result = await search(query, rain)
                assert result["ranked"][0]["name"] == venue_name, result
                assert result["ranked"][0]["paired_menu"]["name"] == paired_name, result
            assert result["ranked"][0]["weather_match"]["label"] == "비 오는 날 · 아늑한 · 해물파전", result
            print("PASS 장소 생략 맥주 · 을지로 생맥주/꼬치 · 서촌 커피/쿠키 · 마포구 막걸리/해물파전")

            explicit = await search("성수카페거리 맥주랑 치킨", rain)
            chicken_venue, = explicit["ranked"]
            assert chicken_venue["name"] == "서울브루어리 성수", explicit
            assert len(chicken_venue["matched_orders"]) == 2 and "paired_menu" not in chicken_venue, explicit
            shorthand = await search("성수카페거리 치맥", rain)
            assert shorthand["ranked"] == explicit["ranked"], shorthand
            pancake = await search("마포구 막걸리랑 전", rain)
            assert len(pancake["ranked"][0]["matched_orders"]) == 2, pancake
            nonalcohol = await search("마포구 논알콜 칵테일 한 잔", rain)
            assert nonalcohol["empty_reason"] == "NO_MENU" and nonalcohol["request"]["venue_category"] is None, nonalcohol
            other = await search("성수카페거리 맥주랑 국물", rain)
            assert [row["name"] for row in other["ranked"]] == ["성수오뎅"], other
            assert "paired_menu" not in other["ranked"][0], other
            beer_only = await search("성수카페거리 맥주만 땡겨", rain)
            assert beer_only["ranked"] and all("paired_menu" not in row for row in beer_only["ranked"]), beer_only
            typed_orders = {"region": "을지로", "venue_category": "PUB", "visit_at": rain.isoformat(),
                            "orders": [{"types": ["menu_beer"]}]}
            both_typed = await call(client, "recommend_venues", {
                **typed_orders, "request_text": "성수카페거리 맥주랑 치킨",
            })
            assert both_typed["ranked"] == explicit["ranked"], both_typed
            beer_only_typed = await call(client, "recommend_venues", {
                **typed_orders, "request_text": "성수카페거리 카페에서 모임 끝나고 맥주만 마시고 싶어",
            })
            assert beer_only_typed["ranked"] == beer_only["ranked"], beer_only_typed
            limited = await search("성수카페거리 맥주 땡긴다 2만원 이하", rain)
            assert [row["name"] for row in limited["ranked"]] == ["성수오뎅"], limited
            assert limited["ranked"][0]["budget_example"]["amount_krw"] == 16900, limited
            assert "paired_menu" not in limited["ranked"][0], limited
            confirmed_budget = await search("을지로 생맥주 한 잔 1만원 이하", rain)
            confirmed, = confirmed_budget["ranked"]
            assert confirmed["budget_example"]["amount_krw"] == 9800, confirmed
            assert confirmed["paired_menu"]["name"] in {item["name"] for item in confirmed["budget_example"]["items"]}, confirmed
            print("PASS 지정한 메뉴와 필수 예산 우선 · 주문 예시로 검증된 조합만 표시")

            sunny, rainy = compared["을지로 카페"]
            for at, expected in ((first, sunny), (rain, rainy)):
                typed = await call(client, "recommend_venues", {
                    "region": "을지로", "venue_category": "CAFE", "preferred_moods": ["아늑한"],
                    "visit_at": at.isoformat(),
                })
                assert [row["venue_id"] for row in typed["ranked"]] == [
                    row["venue_id"] for row in expected["ranked"]
                ], typed
            print("PASS 자연어와 구조화 조건의 동일 날씨 순위")

            for at in (first, rain):
                for query, name, amount in (
                    ("성수카페거리 6500원 이하 레트로 카페", "성수 대림창고", 6500),
                    ("성수카페거리 2만원 이하 아늑한 술집", "성수오뎅", 16900),
                    ("을지로 9800원 이하 술집", "토리키조쿠 을지로3가점", 9800),
                ):
                    result = await search(query, at)
                    venue, = result["ranked"]
                    example = venue["budget_example"]
                    assert venue["name"] == name and example["amount_krw"] == amount, result
                    assert sum(item["price_krw"] for item in example["items"]) == amount, result
                    assert venue["avg_budget_krw"] is None and result["request"]["budget_mode"] == "REQUIRE", result

                for query in ("성수카페거리 6500원 미만 레트로 카페", "을지로 9800원 미만 술집"):
                    excluded = await search(query, at)
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
        wrong_plan = await call(client, "recommend_venues", {
            **base, "request_text": "종로구에서 커피와 쿠키 있는 카페",
            "venue_category": "PUB", "orders": [{"types": ["커피", "쿠키"]}],
        })
        assert wrong_plan["request"]["region"] == "종로구", wrong_plan
        assert wrong_plan["request"]["venue_category"] == "CAFE", wrong_plan
        assert [order["types"] for order in wrong_plan["request"]["orders"]] == [["menu_coffee"], ["menu_cookie"]]
        assert [row["venue_id"] for row in wrong_plan["ranked"]] == [venue["venue_id"]], wrong_plan
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


async def verify_public_catalog_refresh():
    from venue_nearby import ADMIN_MAPPING

    region_ids = {
        "seoul": "region:1168064000",
        "songdo": "region:2818582000",
        "suwon": "region:4111573000",
    }
    mapping = json.loads(ADMIN_MAPPING.read_text(encoding="utf-8"))
    region_rows = {f"region:{row['region_code']}": row for row in mapping["regions"]}
    locations = {
        key: {"region_id": region_id, "nx": region_rows[region_id]["nx"], "ny": region_rows[region_id]["ny"]}
        for key, region_id in region_ids.items()
    }

    def venue(venue_id, area, mood, budget, *, is_public=True):
        return {
            "venue_id": venue_id, "name": f"시험 카페 {venue_id}", "address": "시험 주소",
            "region_id": region_ids[area], "venue_category": "CAFE", "is_public": is_public,
            "mood_tag_ids": [mood], "avg_budget_krw": budget,
        }

    def menu(venue_id, name, tag, price, *, is_public=True):
        return {
            "menu_id": f"{venue_id}-{tag}", "venue_id": venue_id, "name": name, "is_public": is_public,
            "menu_type_tag_ids": [tag], "price_krw": price,
        }

    seoul = venue("public-seoul", "seoul", "mood_daylight", 7000)
    unrelated = venue("public-cake", "songdo", "mood_cozy", 7000)
    hidden = venue("private-venue", "songdo", "mood_daylight", 7000, is_public=False)
    venues = [seoul, unrelated, hidden]
    menus = [
        menu("public-seoul", "아메리카노", "menu_americano", 5000),
        menu("public-cake", "치즈 케이크", "menu_cake", 6000),
        menu("public-cake", "미공개 콜드브루", "menu_cold_brew", 5000, is_public=False),
        menu("private-venue", "핸드드립", "menu_hand_drip", 5000),
    ]

    with TemporaryDirectory() as directory, project_fixture(locations) as (url, first, calls):
        path = Path(directory) / "public_catalog.json"

        def publish(version, *, omit_preview=False, **metadata):
            pending = path.with_suffix(".next")
            fields = {} if omit_preview else {"preview_only": False}
            pending.write_text(json.dumps({"version": version, "venues": venues, "menus": menus, **fields, **metadata},
                                          ensure_ascii=False), encoding="utf-8")
            pending.replace(path)

        publish("public-1")
        async with session_for("--public-catalog", str(path), "--weather-base-url", url) as client:
            async def search(query, at=first, **filters):
                return await call(client, "recommend_venues", {
                    "query": query, "visit_at": at.isoformat(), **filters,
                })

            songdo_query = "인천 송도 커피 카페"
            suwon_query = "경기 수원시 팔달구 커피 카페"
            empty = await search(songdo_query)
            assert empty["status"] == "NO_MATCH" and empty["empty_reason"] == "NO_MENU", empty
            assert empty["request"]["location"]["province"] == "인천광역시", empty
            missing = await search(suwon_query)
            assert missing["empty_reason"] == "NO_REGISTERED_VENUES" and not calls, missing
            all_coffee = await search("수도권 커피 카페")
            assert [row["venue_id"] for row in all_coffee["ranked"]] == ["public-seoul"], all_coffee
            print("PASS 공개 스냅샷 · 서울 대조군 · 미공개 매장·메뉴와 불일치 메뉴 제외")

            sunny = venue("public-songdo-sunny", "songdo", "mood_daylight", 7000)
            rainy = venue("public-songdo-rainy", "songdo", "mood_cozy", 8500)
            suwon = venue("public-suwon", "suwon", "mood_calm", 8000)
            venues.extend([sunny, rainy, suwon])
            menus.extend([
                menu(sunny["venue_id"], "아이스 콜드브루", "menu_cold_brew", 5500),
                menu(rainy["venue_id"], "따뜻한 핸드드립", "menu_hand_drip", 6500),
                menu(suwon["venue_id"], "아메리카노", "menu_americano", 5000),
                menu(suwon["venue_id"], "초코 쿠키", "menu_cookie", 3000),
            ])
            publish("public-2")
            options = await call(client, "list_venue_tags", {})
            assert {"수도권", "인천 부평구", "인천 연수구 송도1동", "경기도 수원시 팔달구 인계동"} <= set(options["regions"])
            dry = await search(songdo_query)
            rain = await search(songdo_query, first + timedelta(hours=1))
            assert dry["catalog_source"] == "PUBLIC_SNAPSHOT" and dry["catalog_version"] == "public-2", dry
            assert dry["weather_mapping_version"] == mapping["version"], dry
            for result, lead, condition, evidence, price in (
                (dry, sunny, "CLEAR", "아이스 콜드브루", 5500),
                (rain, rainy, "RAIN", "따뜻한 핸드드립", 6500),
            ):
                assert result["forecast_status"] == "AVAILABLE" and len(result["ranked"]) == 2, result
                assert {row["venue_id"] for row in result["ranked"]} == {sunny["venue_id"], rainy["venue_id"]}, result
                first_row = result["ranked"][0]
                assert first_row["venue_id"] == lead["venue_id"] and first_row["province"] == "인천광역시", first_row
                assert first_row["forecast"]["condition"] == condition, first_row
                assert first_row["forecast"]["region_id"] == region_ids["songdo"], first_row
                assert evidence in first_row["weather_match"]["label"], first_row
                assert first_row["matched_orders"][0][0]["price_krw"] == price, first_row

            no_forecast = await search(songdo_query, first + timedelta(hours=12))
            assert no_forecast["forecast_status"] == "NO_FORECAST" and len(no_forecast["ranked"]) == 2, no_forecast
            assert all("weather_match" not in row for row in no_forecast["ranked"]), no_forecast
            mood_required = await search("인천 송도 아늑한 커피 카페")
            assert [row["venue_id"] for row in mood_required["ranked"]] == [rainy["venue_id"]], mood_required
            budget_required = await search(songdo_query, first + timedelta(hours=1),
                                           budget={"max_krw": 7000}, budget_mode="REQUIRE")
            assert [row["venue_id"] for row in budget_required["ranked"]] == [sunny["venue_id"]], budget_required
            for amount in ("8천원", "팔천 원"):
                enforced = await call(client, "recommend_venues", {
                    "region": "서울 강남구", "venue_category": "CAFE", "visit_at": first.isoformat(),
                    "orders": [{"types": ["커피"]}], "budget": {"max_krw": 9000}, "budget_mode": "PREFER",
                    "request_text": f"인천 송도 커피 카페, 한 사람당 {amount}을 넘지 않는 곳",
                })
                assert [row["venue_id"] for row in enforced["ranked"]] == [sunny["venue_id"]], enforced
                assert enforced["request"]["budget"] == {"min_krw": None, "max_krw": 8000}, enforced
                assert enforced["request"]["budget_mode"] == "REQUIRE", enforced
            print("PASS 맑음·비·예보 없음 · 날씨는 분위기와 예산 필수 조건을 통과한 후보의 순위에만 적용")

            new_suwon = await search(suwon_query)
            suwon_row, = new_suwon["ranked"]
            assert suwon_row["venue_id"] == suwon["venue_id"] and suwon_row["province"] == "경기도", suwon_row
            assert suwon_row["forecast"]["region_id"] == region_ids["suwon"], suwon_row
            typed = await call(client, "recommend_venues", {
                "region": "경기도 수원시 팔달구 인계동", "venue_category": "CAFE",
                "visit_at": first.isoformat(), "orders": [{"types": ["menu_coffee"]}],
            })
            assert [row["venue_id"] for row in typed["ranked"]] == [suwon["venue_id"]], typed
            paired = await search("경기 수원시 팔달구 커피 땡겨")
            assert paired["ranked"][0]["paired_menu"]["name"] == "초코 쿠키", paired
            exact_budget = await search("경기 수원시 팔달구 8천원 이하 차분한 커피 카페")
            assert exact_budget["ranked"][0]["avg_budget_krw"] == 8000, exact_budget
            below_budget = await search("경기 수원시 팔달구 8천원 미만 차분한 커피 카페")
            assert below_budget["empty_reason"] == "NO_BUDGET", below_budget
            no_mood = await search("경기 수원시 팔달구 활기찬 커피 카페")
            assert no_mood["empty_reason"] == "NO_MOOD", no_mood
            print("PASS 같은 MCP 연결 · 송도·수원 신규 반영 · 예보·메뉴·분위기·예산 경계")

            sunny["is_public"] = False
            menus[-2]["is_public"] = False
            publish("public-3")
            after = await search(songdo_query)
            assert after["catalog_version"] == "public-3", after
            assert [row["venue_id"] for row in after["ranked"]] == [rainy["venue_id"]], after
            assert (await search(suwon_query))["empty_reason"] == "NO_MENU"

            path.write_text("{", encoding="utf-8")
            failed = await client.call_tool("recommend_venues", arguments={"query": songdo_query})
            assert failed.isError, failed
            publish("public-4")
            recovered = await search(songdo_query)
            assert recovered["catalog_version"] == "public-4" and recovered["ranked"], recovered
            print("PASS 공개 해제 즉시 반영 · 읽기 실패 후 같은 연결에서 새 스냅샷으로 복구")

            publish("missing-preview-flag", omit_preview=True)
            before = len(calls)
            missing_flag = await client.call_tool("recommend_venues", arguments={"query": songdo_query})
            assert missing_flag.isError and len(calls) == before, missing_flag
            publish("public-after-missing")
            assert (await search(songdo_query))["catalog_version"] == "public-after-missing"
            print("PASS 운영 모드에서 preview_only 누락 차단 · 명시적 false로 복구")

            preview = {
                "preview_only": True, "independently_verified_menus": 0,
                "source_basis": "로컬 팀 CSV", "budget_basis": "원본 평균예산 참고",
            }
            publish("preview-5", **preview)
            before = len(calls)
            blocked = await client.call_tool("recommend_venues", arguments={"query": songdo_query})
            assert blocked.isError and len(calls) == before, blocked

        async with session_for("--public-catalog", str(path), "--allow-preview-catalog") as client:
            options = await call(client, "list_venue_tags", {})
            preview_result = await call(client, "recommend_venues", {"query": songdo_query})
            assert options["catalog_metadata"] == preview_result["catalog_metadata"] == preview, preview_result
            assert preview_result["catalog_version"] == "preview-5" and preview_result["ranked"], preview_result
            publish("legacy-local-preview", omit_preview=True)
            legacy = await call(client, "recommend_venues", {"query": songdo_query})
            assert legacy["catalog_version"] == "legacy-local-preview" and legacy["ranked"], legacy
            print("PASS 시연 스냅샷 기본 차단 · 로컬 허용 시 출처·예산 근거와 검증 상태 보존")


async def verify_offline():
    await verify_project()
    await verify_csv_sample()
    await verify_concierge_ranking()
    await verify_unverified_intent()
    await verify_search_location_and_budget()
    await verify_seoul_place_index()
    await verify_curated_recommendations()
    await verify_typed_area_and_menu()
    await verify_partial_forecast_deadline()
    await verify_public_catalog_refresh()


async def verify_live(dry, rain, missing):
    async with session_for("--demo-open-meteo") as client:
        for visit, expectation in ((dry, "DRY"), (rain, "RAIN"), (missing, "NO_FORECAST")):
            args = output(visit, region="발산1동")
            result = await call(client, "recommend_venues", args)
            row, = result["ranked"]
            forecast = row["forecast"]
            assert forecast["provider"] == "open_meteo", result
            assert forecast["state"] == expectation, (args, forecast)
            if expectation == "NO_FORECAST":
                assert forecast["reason"] == "TIME_NOT_COVERED", forecast
            print_case(f"공개 예보 {visit.isoformat()}", result)
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
