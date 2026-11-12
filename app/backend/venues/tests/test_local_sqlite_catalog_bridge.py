from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings

from backend.venues.models.catalog import VenueCatalogRecord
from backend.venues.repositories.curated_venue_detail import local_catalog_detail


@override_settings(LOCAL_DEMO=True, MEMBER_LOCAL_PREVIEW=True, PLATFORM_ENABLED=False)
class LocalSQLiteCatalogBridgeTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "catalog.sqlite3"
        self.row = {
            "venue_id": "VN_IC_LOCAL", "매장명": "인천 테스트 카페",
            "도로명주소": "인천광역시 연수구 테스트로 1", "자치구": "연수구", "행정동": "송도동",
            "업종구분": "카페", "세부카테고리": "카페", "영업상태_코드": "1",
            "1인_평균예산(원)": "9000", "위도_lat": "37.4", "경도_lng": "126.66",
        }
        self.menu = {
            "menu_id": "MN_LOCAL", "venue_id": "VN_IC_LOCAL", "메뉴명": "테스트 라떼",
            "가격(원)": "5000", "대표메뉴여부": "Y",
        }
        raw = json.dumps(self.row, ensure_ascii=False)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE venues (venue_id TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE TABLE menus (menu_id TEXT NOT NULL, venue_id TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE INDEX venues_id ON venues (venue_id);
                CREATE INDEX menus_venue_id ON menus (venue_id);
            """)
            db.execute("INSERT INTO metadata VALUES (?, ?)",
                       ("catalog", json.dumps({"schema": 1, "version": "local", "preview_only": False})))
            db.execute("INSERT INTO metadata VALUES (?, ?)",
                       ("venue_rows_gzip", sqlite3.Binary(gzip.compress((raw + "\n").encode()))))
            db.execute("INSERT INTO venues VALUES (?, ?)", (self.row["venue_id"], raw))
            db.execute("INSERT INTO menus VALUES (?, ?, ?)",
                       (self.menu["menu_id"], self.menu["venue_id"], json.dumps(self.menu, ensure_ascii=False)))
            db.commit()
        environment = patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(self.path)})
        environment.start()
        self.addCleanup(environment.stop)

    @patch("backend.venues.views.list_view.check_and_send_nearby_event_promo")
    def test_local_list_and_detail_use_the_same_sqlite_venue(self, promo):
        listing = self.client.get("/api/venues", {"sigungu": "인천 연수구"})
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual([row["venue_id"] for row in listing.json()["venues"]], ["VN_IC_LOCAL"])
        detail = self.client.get("/api/venues/VN_IC_LOCAL")
        self.assertEqual(detail.status_code, 200, detail.content)
        venue = detail.json()["venue"]
        self.assertEqual((venue["venue_id"], venue["venue_name"]), ("VN_IC_LOCAL", "인천 테스트 카페"))
        self.assertEqual({row["menu_id"] for tab in venue["menu_tabs"] for row in tab["items"]}, {"MN_LOCAL"})
        self.assertFalse(venue["has_verified_menus"])
        promo.assert_called_once()

    def test_sqlite_is_not_read_as_json(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("SQLite는 JSON으로 읽으면 안 됩니다.")):
            self.assertIsNone(local_catalog_detail("VN_IC_LOCAL"))

    def test_unknown_or_unavailable_catalog_never_falls_back_to_demo_records(self):
        VenueCatalogRecord.objects.create(venue_id="owner_private", dataset_version="owner_direct",
                                          venue_name="심사 전 매장", gu_name="연수구")
        self.assertEqual(self.client.get("/api/venues/owner_private").status_code, 404)
        with patch.dict(os.environ, {"VENUE_MCP_PUBLIC_CATALOG": str(self.path.parent / "missing.sqlite3")}):
            self.assertEqual(self.client.get("/api/venues/VN_IC_LOCAL").status_code, 503)
            self.assertEqual(self.client.get("/api/venues", {"sigungu": "연수구"}).status_code, 503)
