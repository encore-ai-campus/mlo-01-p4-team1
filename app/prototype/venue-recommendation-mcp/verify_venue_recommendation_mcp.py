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
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from venue_forecast import KST, timestamp


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "venue_recommendation_mcp.py"
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
    sample_data = ROOT.parents[1] / "backend" / "venues" / "sample_data"
    catalog_path = ROOT / "fixtures" / "csv_sample_catalog.json"
    locations_path = ROOT / "fixtures" / "csv_sample_locations.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    locations = json.loads(locations_path.read_text(encoding="utf-8"))["locations"]
    venue, = catalog["venues"]
    venue_id = venue["venue_id"]

    with (sample_data / "seoul_venues_demo_sample.csv").open(encoding="utf-8-sig", newline="") as source:
        assert any(row["venue_id"] == venue_id for row in csv.DictReader(source))
    with (sample_data / "seoul_venue_menus_demo_sample.csv").open(encoding="utf-8-sig", newline="") as source:
        original_menus = {
            row["menu_id"]: row["메뉴명"] for row in csv.DictReader(source) if row["venue_id"] == venue_id
        }
    assert all(original_menus[item["menu_id"]] == item["name"] for item in catalog["menus"])

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


async def verify_offline():
    await verify_project()
    await verify_csv_sample()


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
