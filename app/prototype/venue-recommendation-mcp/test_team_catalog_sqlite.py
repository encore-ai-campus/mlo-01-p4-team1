import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from build_team_catalog_sqlite import build
from venue_nearby import SeoulPlaceIndex
from venue_recommendation_mcp import TAG_FILE
from venue_team_catalog import TeamCatalogReader
from verify_tag_weather import read_tags


class TeamCatalogSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.venue = {
            "venue_id": "v1", "매장명": "테스트 술집", "도로명주소": "서울특별시 마포구 검증로 1",
            "업종구분": "술집", "세부카테고리": "요리 주점", "영업상태_코드": "OPEN",
            "자치구": "마포구", "행정동": "서교동", "행정동코드": "1144066000",
            "매장분위기": "아늑한", "1인_평균예산(원)": "17000", "출처_URL": "https://example.com/review",
            "매장특징_조사": "2차로 들렀어요.", "대표메뉴_조사": "생맥주, 감자튀김",
        }
        self.menus = [
            {"menu_id": "m1", "venue_id": "v1", "매장명": "테스트 술집", "메뉴명": "생맥주", "가격(원)": "6000"},
            {"menu_id": "m2", "venue_id": "v1", "매장명": "테스트 술집", "메뉴명": "감자튀김", "가격(원)": "8000"},
        ]
        for name, rows in (("seoul_venues_full.csv", [self.venue]), ("seoul_venue_menus_full.csv", self.menus)):
            with (self.root / name).open("w", encoding="utf-8-sig", newline="") as target:
                writer = csv.DictWriter(target, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        self.originals = {path: path.read_bytes() for path in self.root.glob("*.csv")}
        self.output = self.root / "catalog.sqlite3"

    def test_all_original_rows_and_values_are_kept_and_searchable(self):
        result = build([self.root], self.output)
        self.assertFalse(result["preview_only"])
        with closing(sqlite3.connect(self.output)) as db:
            raw_venue = json.loads(db.execute("SELECT raw FROM venues").fetchone()[0])
            raw_menus = [json.loads(row[0]) for row in db.execute("SELECT raw FROM menus ORDER BY menu_id")]
        self.assertEqual(raw_venue, self.venue)
        self.assertEqual(raw_menus, self.menus)
        self.assertEqual({path: path.read_bytes() for path in self.originals}, self.originals)
        tags, labels, ancestors = read_tags(TAG_FILE)
        reader = TeamCatalogReader(self.output, SeoulPlaceIndex(capital=True).weather_regions, tags, labels, ancestors)
        catalog, locations = reader.read()
        venue = catalog["venues"][0]
        self.assertEqual((venue["venue_id"], venue["avg_budget_krw"]), ("v1", 17000))
        self.assertIn("visit_second_round", venue["visit_purposes"])
        menus = reader.menus_for([venue], catalog["_identity"])
        self.assertEqual([(menu["name"], menu["price_krw"]) for menu in menus], [("생맥주", 6000), ("감자튀김", 8000)])
        self.assertTrue(all(menu.get("evidence") for menu in menus))
        self.assertEqual(locations["v1"]["province"], "서울특별시")

    def test_replacement_invalidates_the_previous_read(self):
        build([self.root], self.output)
        tags, labels, ancestors = read_tags(TAG_FILE)
        reader = TeamCatalogReader(self.output, SeoulPlaceIndex(capital=True).weather_regions, tags, labels, ancestors)
        catalog, _ = reader.read()
        build([self.root], self.output, replace=True)
        with self.assertRaisesRegex(ValueError, "교체"):
            reader.menus_for(catalog["venues"], catalog["_identity"])
        self.assertEqual(reader.read()[0]["version"], catalog["version"])


if __name__ == "__main__":
    unittest.main()
