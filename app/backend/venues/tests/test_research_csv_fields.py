import json
import unittest

from backend.venues.repositories.csv_lazy_repo import CsvLazyRepository


class ResearchCsvFieldTests(unittest.TestCase):
    def setUp(self):
        self.repo = CsvLazyRepository.__new__(CsvLazyRepository)
        self.row = {
            'venue_id': 'v1', '매장명': '독립카페', '도로명주소': '서울특별시 마포구 길 1',
            '자치구': '마포구', '업종구분': '카페', '매장분위기': '차분한|레트로',
            '1인_평균예산(원)': '15000', '영업시간': '10:00-20:00',
        }

    def test_research_fields_survive_in_detail_payload_without_overwriting_existing_fields(self):
        venue = self.repo._cast_venue_row({**self.row,
            '매장특징_조사': '작은 로스터리', '대표메뉴_조사': '핸드드립|쿠키',
            '좌석_편의_조사': '창가 좌석', '출처_URL': 'https://one.example/a|https://two.example/b',
            '조사확인일': '2026-11-11', '제한사항': '2025년 후기 기준',
        })
        payload = json.loads(json.dumps({'success': True, 'venue': {**venue}}, ensure_ascii=False))
        self.assertEqual(payload['venue']['research'], {
            'description': '작은 로스터리', 'representative_menus': '핸드드립|쿠키',
            'seating': '창가 좌석', 'sources': ['https://one.example/a', 'https://two.example/b'],
            'checked_on': '2026-11-11', 'limitations': '2025년 후기 기준',
        })
        self.assertEqual(venue['mood_tags'], ['차분한', '레트로'])
        self.assertEqual(venue['avg_price'], 15000)
        self.assertEqual(venue['open_hours'], '10:00-20:00')

    def test_absent_and_blank_research_fields_do_not_change_original_record(self):
        before = self.repo._cast_venue_row(self.row)
        after = self.repo._cast_venue_row({**self.row, '매장특징_조사': '', '출처_URL': ''})
        self.assertEqual(before, after)
        self.assertNotIn('research', after)

    def test_existing_menu_price_is_unchanged(self):
        menu = self.repo._cast_menu_row({'venue_id': 'v1', 'menu_id': 'm1', '메뉴명': '아메리카노', '가격(원)': '5000'})
        self.assertEqual(menu['price'], 5000)
        self.assertEqual(menu['menu_name'], '아메리카노')

    def test_checked_mood_replaces_display_tags_only_when_source_present(self):
        checked = self.repo._cast_venue_row({**self.row, '매장분위기_조사': '조용한|정원', '출처_URL': 'https://example.com/r'})
        self.assertEqual(checked['mood_tags'], ['조용한', '정원'])
        missing_source = self.repo._cast_venue_row({**self.row, '매장분위기_조사': '조용한'})
        self.assertEqual(missing_source['mood_tags'], ['차분한', '레트로'])
