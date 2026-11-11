import json
from pathlib import Path
import unittest
from unittest.mock import patch

from prepare_team_catalog import apply_research, _reviewed_moods, _reviewed_menu_names
from prepare_team_catalog import TAG_FILE
from verify_tag_weather import read_tags


class ResearchOverlayTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'preview_only': True, 'venues': [
            {'venue_id': 'a', 'name': '첫 매장', 'address': '서울특별시 마포구 어울마당로 1', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
            {'venue_id': 'b', 'name': '둘째 매장', 'address': '경기도 하남시 미사대로 2', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
            {'venue_id': 'c', 'name': '셋째 매장', 'address': '인천광역시 연수구 송도동 3', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
        ], 'menus': [{'venue_id': 'c', 'menu_id': 'invented'}]}
        self.local = [{'venue_id':'a', '매장명':'첫 매장', '도로명주소':'서울 마포구 어울마당로 1', '매장분위기':'아늑한 | 근거없는태그', '매장소개_후기':'작은 카페', '대표메뉴_후기':'아메리카노 | 김치전', '조사상태':'후기_단일출처', '확인일':'2026-11-11','제한사항':'후기 한 건'}]
        self.sources = [{'venue_id':'a','출처구분':'VISITOR','출처_URL':'https://example.com/review','매장일치근거':'주소 일치','채택상태':'보완안_근거'}]
        self.home = [{'venue_id':'b','매장명':'둘째 매장','주소':'경기 하남시 미사대로 2','분위기':'차분한','매장 특징':'정원','확인된 메뉴':'에그타르트','확인일':'2026-11-11','제한사항':'메뉴 변동 가능','출처목록':json.dumps([{'출처 URL':'https://example.com/home','출처 유형':'방문 후기 원문','같은 매장이라고 판단한 근거':'주소 일치'}]),'보류목록':'[]','검토사유':''}]

    def apply(self):
        rows = {'local.csv': self.local, 'sources.csv': self.sources, 'home.csv': self.home}
        with patch('prepare_team_catalog.read_export', side_effect=lambda path, required: (rows[path.name], {})):
            return apply_research(self.snapshot, Path('local.csv'), Path('sources.csv'), Path('home.csv'))

    def test_verified_data_only_and_no_unverified_prices(self):
        out = self.apply()
        a, b, c = out['venues']
        self.assertEqual(a['mood_tag_ids'], ['mood_cozy'])
        self.assertEqual(b['mood_tag_ids'], ['mood_calm'])
        self.assertEqual(c['mood_tag_ids'], [])
        self.assertTrue(all(v['avg_budget_krw'] is None for v in out['venues']))
        self.assertEqual(a['research']['sources'], ['https://example.com/review'])
        self.assertEqual({m['name'] for m in out['menus']}, {'아메리카노', '김치전', '에그타르트'})
        self.assertTrue(all(m['price_krw'] is None for m in out['menus']))
        self.assertNotIn('research', c)

    def test_wrong_address_and_held_are_never_published(self):
        self.local[0]['도로명주소'] = '서울 마포구 어울마당로 99'
        self.home[0]['보류목록'] = '[{"상태":"보류"}]'
        out = self.apply()
        self.assertEqual(out['menus'], [])
        self.assertEqual(out['research_audit']['identity_conflict'], 1)
        self.assertEqual(out['research_audit']['held_or_invalid'], 1)

    def test_duplicate_identity_and_non_preview_fail(self):
        self.home[0]['venue_id'] = 'a'
        with self.assertRaises(ValueError): self.apply()
        self.home[0]['venue_id'] = 'b'
        self.snapshot['preview_only'] = False
        with self.assertRaises(ValueError): self.apply()

    def test_editorial_only_is_not_a_visitor(self):
        self.sources[0]['출처구분'] = 'EDITORIAL'
        out = self.apply()
        self.assertNotIn('research', out['venues'][0])
        self.assertEqual(out['research_audit']['missing_visitor_source'], 1)

    def test_sentence_mood_mapping_and_negation(self):
        _, labels, _ = read_tags(TAG_FILE)
        moods = labels['MOOD']
        self.assertEqual(set(_reviewed_moods('전통 한옥과 정원이 어우러진 분위기', moods)), {'mood_hanok', 'mood_garden'})
        self.assertEqual(set(_reviewed_moods('아늑하고 따뜻한 찻집', moods)), {'mood_cozy'})
        self.assertEqual(set(_reviewed_moods('흰 벽과 나무 가구의 차분한 공간', moods)), {'mood_calm'})
        self.assertEqual(_reviewed_moods('조용하지 않고 시끄러운 공간', moods), [])
        self.assertEqual(_reviewed_moods('아늑한 분위기는 아니에요', moods), [])
        self.assertEqual(_reviewed_moods('정원은 없고 한옥 여부는 미확인', moods), [])
        self.assertEqual(_reviewed_moods('넓은 실내', moods), [])

    def test_menu_comma_splits_without_splitting_prices_or_parentheses(self):
        self.assertEqual(_reviewed_menu_names('라떼, 버터풀라떼, 소금빵'), ['라떼', '버터풀라떼', '소금빵'])
        self.assertEqual(_reviewed_menu_names('라떼 (2,000원), 쿠키'), ['라떼 (2,000원)', '쿠키'])
        self.home[0]['확인된 메뉴'] = '라떼 (2,000원), 쿠키 | 라떼'
        out = self.apply()
        names = {m['name'] for m in out['menus'] if m['venue_id'] == 'b'}
        self.assertEqual(names, {'쿠키', '라떼'})


if __name__ == '__main__':
    unittest.main()
