"""Offline checks for paired CSV import and local-preview provenance."""

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from prepare_team_catalog import MENU_FIELDS, VENUE_FIELDS, prepare, source_pairs, write_snapshot


def venue(identifier, *, region="1144066000", district="마포구", dong="서교동", address="서울특별시 마포구 검증로 1"):
    return {
        **dict.fromkeys(VENUE_FIELDS, ""),
        "venue_id": identifier, "매장명": f"검증매장 {identifier}", "업종구분": "카페",
        "세부카테고리": "카페", "자치구": district, "행정동": dong, "행정동코드": region,
        "도로명주소": address, "영업상태_코드": "OPEN", "매장분위기": "잔잔한",
        "1인_평균예산(원)": "15,000",
    }


def menu(parent, identifier, name="김치전골"):
    return {
        **dict.fromkeys(MENU_FIELDS, ""),
        "menu_id": identifier, "venue_id": parent["venue_id"], "매장명": parent["매장명"],
        "자치구": parent["자치구"], "행정동": parent["행정동"], "메뉴명": name, "가격(원)": "8,000",
    }


def pair(directory, prefix, venues, menus):
    for suffix, records, fields in (("venues", venues, VENUE_FIELDS), ("menus", menus, MENU_FIELDS)):
        with (directory / f"{prefix}_{suffix}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(fields))
            writer.writeheader()
            writer.writerows(records)


class TeamCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="venue-import-check-")
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def test_new_csv_pairs_preserve_ids_and_capital_provinces(self):
        rows = [
            venue("seoul"),
            venue("incheon", region="2818582000", district="연수구", dong="송도1동", address="인천광역시 연수구 검증로 1"),
            venue("gyeonggi", region="4111573000", district="수원시팔달구", dong="인계동", address="경기도 수원시 팔달구 검증로 1"),
        ]
        for index, row in enumerate(rows):
            pair(self.directory, f"추가자료{index}", [row], [menu(row, f"menu{index}")])
        data = prepare(self.directory)
        actual = {row["venue_id"]: row["province"] for row in data["venues"]}
        self.assertEqual(actual, {"seoul": "서울특별시", "incheon": "인천광역시", "gyeonggi": "경기도"})
        self.assertEqual({row["venue_id"] for row in data["menus"]}, set(actual))
        self.assertTrue(data["preview_only"])
        self.assertEqual(data["independently_verified_menus"], 0)
        self.assertEqual({row["avg_budget_krw"] for row in data["venues"]}, {15000})
        self.assertEqual({row["price_krw"] for row in data["menus"]}, {8000})
        for row in data["menus"]:
            self.assertEqual(row["menu_type_tag_ids"], ["menu_jeongol"])
            self.assertIn("메뉴판 미확인", row["name"])

    def test_closed_conflicting_region_and_wrong_menu_parent_are_excluded(self):
        good = venue("good")
        closed = {**venue("closed"), "영업상태_코드": "CLOSED"}
        wrong_province = venue("wrong", address="인천광역시 연수구 검증로 1")
        wrong_menu = {**menu(good, "wrong-menu"), "매장명": "다른매장"}
        orphan = {**menu(good, "orphan"), "venue_id": "not-in-source"}
        pair(self.directory, "자료", [good, closed, wrong_province],
             [menu(good, "good-menu"), menu(closed, "closed-menu"), menu(wrong_province, "wrong-region-menu"), wrong_menu, orphan])
        data = prepare(self.directory)
        self.assertEqual([row["venue_id"] for row in data["venues"]], ["good"])
        self.assertEqual([row["menu_id"] for row in data["menus"]], ["good-menu"])
        self.assertEqual(sum(data["audit"]["자료"]["excluded_menus"].values()), 4)

    def test_unknown_menu_does_not_acquire_a_guessed_food_tag(self):
        row = venue("v1")
        pair(self.directory, "자료", [row], [menu(row, "m1", "오늘의 특별 메뉴")])
        data = prepare(self.directory)
        self.assertEqual(data["menus"][0]["menu_type_tag_ids"], [])
        self.assertEqual(data["menus"][0]["cuisine_tag_ids"], [])

    def test_invalid_prices_remain_unknown(self):
        row = {**venue("v1"), "1인_평균예산(원)": "1,5,000"}
        item = {**menu(row, "m1"), "가격(원)": "-1000"}
        pair(self.directory, "자료", [row], [item])
        data = prepare(self.directory)
        self.assertIsNone(data["venues"][0]["avg_budget_krw"])
        self.assertIsNone(data["menus"][0]["price_krw"])

    def test_duplicate_ids_across_files_fail(self):
        row = venue("same-id")
        pair(self.directory, "첫자료", [row], [menu(row, "m1")])
        pair(self.directory, "추가자료", [row], [menu(row, "m2")])
        with self.assertRaisesRegex(ValueError, "중복"):
            prepare(self.directory)

    def test_missing_pairs_and_headers_fail(self):
        with self.assertRaisesRegex(ValueError, "파일 쌍"):
            source_pairs(self.directory)
        (self.directory / "자료_venues.csv").write_text("venue_id\nv1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "파일 쌍"):
            source_pairs(self.directory)
        (self.directory / "자료_menus.csv").write_text("menu_id\nm1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "누락 헤더"):
            prepare(self.directory)

    def test_replacement_is_explicit_and_failed_write_preserves_existing_snapshot(self):
        output = self.directory / "preview.json"
        write_snapshot({"version": "old"}, output)
        with self.assertRaisesRegex(ValueError, "이미"):
            write_snapshot({"version": "new"}, output)
        self.assertEqual(json.loads(output.read_text())["version"], "old")
        with self.assertRaises(TypeError):
            write_snapshot({"not_json": object()}, output, replace=True)
        self.assertEqual(json.loads(output.read_text())["version"], "old")
        write_snapshot({"version": "new"}, output, replace=True)
        self.assertEqual(json.loads(output.read_text())["version"], "new")


if __name__ == "__main__":
    unittest.main()
