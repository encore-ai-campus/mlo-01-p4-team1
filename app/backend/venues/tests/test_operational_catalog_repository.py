from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from backend.venues.repositories.operational_catalog import (
    CatalogUnavailable, get_operational_venue_repo,
)


def venue(identifier, name, address, district, *, category="술집", status="OPEN", latitude="37.55", longitude="126.92"):
    return {
        "venue_id": identifier, "매장명": name, "도로명주소": address, "자치구": district,
        "행정동": "검증동", "업종구분": category, "영업상태_코드": status,
        "위도_lat": latitude, "경도_lng": longitude, "1인_평균예산(원)": "17000",
        "영업시간": "매일 10:00-22:00", "오픈시각": "10:00", "마감시각": "22:00",
        "매장분위기": "아늑한",
    }


def write_catalog(path, venues, menus=(), *, preview_only=False):
    metadata = {"schema": 1, "version": "fixture", "preview_only": preview_only}
    lines = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in venues).encode("utf-8")
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE venues (venue_id TEXT NOT NULL, raw TEXT NOT NULL);
            CREATE TABLE menus (menu_id TEXT NOT NULL, venue_id TEXT NOT NULL, raw TEXT NOT NULL);
        """)
        db.execute("INSERT INTO metadata VALUES (?, ?)", ("catalog", json.dumps(metadata)))
        db.execute("INSERT INTO metadata VALUES (?, ?)", ("venue_rows_gzip", gzip.compress(lines)))
        db.executemany("INSERT INTO venues VALUES (?, ?)", [
            (row["venue_id"], json.dumps(row, ensure_ascii=False)) for row in venues
        ])
        db.executemany("INSERT INTO menus VALUES (?, ?, ?)", [
            (row["menu_id"], row["venue_id"], json.dumps(row, ensure_ascii=False)) for row in menus
        ])
        db.commit()


class OperationalCatalogRepositoryTests(SimpleTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.rows = [
            venue("VN_SL_1", "서울 주점", "서울특별시 중구 검증로 1", "중구"),
            venue("VN_IC_1", "인천 주점", "인천광역시 중구 검증로 1", "중구", latitude="37.47", longitude="126.62"),
            venue("VN_GG_1", "수원 카페", "경기도 수원시 팔달구 검증로 1", "수원시 팔달구", category="카페"),
            venue("closed", "폐업 매장", "서울특별시 중구 검증로 2", "중구", status="CLOSED"),
            venue("facility", "단비 어린이집", "서울특별시 중구 검증로 3", "중구"),
        ]
        self.menu_rows = [
            {"menu_id": "m1", "venue_id": "VN_SL_1", "메뉴명": "생맥주", "가격(원)": "6000", "대표메뉴여부": "Y"},
            {"menu_id": "m2", "venue_id": "VN_SL_1", "메뉴명": "감자튀김", "가격(원)": "8000", "대표메뉴여부": "N"},
            {"menu_id": "other", "venue_id": "VN_IC_1", "메뉴명": "다른 메뉴", "가격(원)": "4000"},
        ]
        write_catalog(self.path, self.rows, self.menu_rows)
        environment = patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_region_and_bbox_do_not_mix_seoul_incheon_or_closed_facilities(self):
        repo = get_operational_venue_repo()
        seoul = repo.get_venues_by_gu("중구")
        incheon = repo.get_venues_by_gu("인천 중구")
        self.assertEqual([row["venue_id"] for row in seoul], ["VN_SL_1"])
        self.assertEqual([row["venue_id"] for row in incheon], ["VN_IC_1"])
        self.assertEqual([row["venue_id"] for row in repo.get_venues_by_gu("수원시")], ["VN_GG_1"])
        self.assertEqual([row["venue_id"] for row in repo.get_venues_by_gu("수원시 팔달구")], ["VN_GG_1"])
        self.assertEqual(
            [row["venue_id"] for row in repo.get_venues_by_bbox(37.45, 37.49, 126.60, 126.65)],
            ["VN_IC_1"],
        )

    def test_detail_and_menus_use_only_catalog_ids_and_original_values(self):
        repo = get_operational_venue_repo()
        found = repo.get_venue_by_id("VN_SL_1")
        self.assertEqual(found["venue_name"], "서울 주점")
        self.assertEqual(found["avg_price"], 17000)
        self.assertEqual(found["phone"], "")
        self.assertIsNone(found["rating"])
        self.assertIsNone(found["review_count"])
        self.assertIsNone(repo.get_venue_by_id("owner_direct_unapproved"))
        self.assertIsNone(repo.get_venue_by_id("facility"))
        self.assertEqual(
            [(row["menu_id"], row["venue_id"], row["menu_name"], row["price"]) for row in repo.get_menus_by_venue("VN_SL_1")],
            [("m1", "VN_SL_1", "생맥주", 6000), ("m2", "VN_SL_1", "감자튀김", 8000)],
        )
        self.assertTrue(all(not row["price_confirmed"] for row in repo.get_menus_by_venue("VN_SL_1")))
        self.assertEqual(repo.get_menus_by_venue("owner_direct_unapproved"), [])
        self.assertEqual([row["menu_id"] for row in repo.get_preview_menus_by_venue(found)], ["m1", "m2"])

    def test_replacement_invalidates_in_flight_snapshot_and_next_request_uses_new_file(self):
        old = get_operational_venue_repo()
        self.assertEqual(len(old.get_venues_by_gu("중구")), 1)
        updated = self.path.with_name("replacement.sqlite3")
        write_catalog(updated, [venue("new", "새 매장", "서울특별시 중구 검증로 9", "중구")])
        os.replace(updated, self.path)
        with self.assertRaises(CatalogUnavailable):
            old.get_menus_by_venue("VN_SL_1")
        with self.assertRaises(CatalogUnavailable):
            old.verify_current()
        self.assertEqual([row["venue_id"] for row in get_operational_venue_repo().get_venues_by_gu("중구")], ["new"])

    def test_unpublished_or_missing_file_fails_without_fallback(self):
        write_catalog(self.path.with_name("preview.sqlite3"), self.rows, preview_only=True)
        for path in (self.path.with_name("preview.sqlite3"), self.path.with_name("missing.sqlite3")):
            with self.subTest(path=path), patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(path)}):
                with self.assertRaises(CatalogUnavailable):
                    get_operational_venue_repo()

    def test_invalid_rating_and_review_count_are_unknown(self):
        invalid = {**self.rows[0], "rating": "NaN", "review_count": "Infinity"}
        replacement = self.path.with_name("invalid-values.sqlite3")
        write_catalog(replacement, [invalid])
        with patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(replacement)}):
            found = get_operational_venue_repo().get_venue_by_id(invalid["venue_id"])
        self.assertIsNone(found["rating"])
        self.assertIsNone(found["review_count"])

    def test_invalid_rows_and_incomplete_database_fail_closed(self):
        malformed = {**self.rows[0], "도로명주소": {"incorrect": "type"}}
        malformed_path = self.path.with_name("malformed.sqlite3")
        write_catalog(malformed_path, [malformed])
        with patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(malformed_path)}):
            with self.assertRaises(CatalogUnavailable):
                get_operational_venue_repo()

        incomplete_path = self.path.with_name("incomplete.sqlite3")
        write_catalog(incomplete_path, self.rows)
        with closing(sqlite3.connect(incomplete_path)) as db:
            db.execute("DROP TABLE menus")
            db.commit()
        with patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(incomplete_path)}):
            with self.assertRaises(CatalogUnavailable):
                get_operational_venue_repo()

    def test_concurrent_requests_each_receive_consistent_data(self):
        def read(_):
            repo = get_operational_venue_repo()
            results = repo.get_venues_by_gu("중구")
            repo.verify_current()
            return [row["venue_id"] for row in results]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(read, range(8))), [["VN_SL_1"]] * 8)
