import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from prepare_team_catalog import apply_research, apply_research_update, _reviewed_moods, _reviewed_menu_names, main, prepare
from prepare_team_catalog import TAG_FILE
from verify_tag_weather import read_tags
from verify_team_catalog import venue, menu


class ResearchOverlayTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'preview_only': True, 'venues': [
            {'venue_id': 'a', 'name': '첫 매장', 'address': '서울특별시 마포구 어울마당로 1', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
            {'venue_id': 'b', 'name': '둘째 매장', 'address': '경기도 하남시 미사대로 2', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
            {'venue_id': 'c', 'name': '셋째 매장', 'address': '인천광역시 연수구 송도동 3', 'mood_tag_ids': ['invented'], 'avg_budget_krw': 10000},
        ], 'menus': [
            {'venue_id': 'a', 'menu_id': 'm1', 'name': '아메리카노 (매장 메뉴판 미확인)', 'source_menu_name': '아메리카노', 'price_krw': 4500},
            {'venue_id': 'c', 'menu_id': 'm2', 'name': '기존 메뉴', 'price_krw': 8000},
        ], 'budget_basis': '원본 예산'}
        self.local = [{'venue_id':'a', '매장명':'첫 매장', '도로명주소':'서울 마포구 어울마당로 1', '매장분위기':'아늑한 | 근거없는태그', '매장소개_후기':'작은 카페', '대표메뉴_후기':'아메리카노 | 김치전', '조사상태':'후기_단일출처', '확인일':'2026-11-11','제한사항':'후기 한 건'}]
        self.sources = [{'venue_id':'a','출처구분':'VISITOR','출처_URL':'https://example.com/review','매장일치근거':'주소 일치','채택상태':'보완안_근거'}]
        self.home = [{'venue_id':'b','매장명':'둘째 매장','주소':'경기 하남시 미사대로 2','분위기':'차분한','매장 특징':'정원','확인된 메뉴':'에그타르트','확인일':'2026-11-11','제한사항':'메뉴 변동 가능','출처목록':json.dumps([{'출처 URL':'https://example.com/home','출처 유형':'방문 후기 원문','같은 매장이라고 판단한 근거':'주소 일치'}]),'보류목록':'[]','검토사유':''}]

    def apply(self):
        rows = {'local.csv': self.local, 'sources.csv': self.sources, 'home.csv': self.home}
        with patch('prepare_team_catalog.read_export', side_effect=lambda path, required: (rows[path.name], {})):
            return apply_research(self.snapshot, Path('local.csv'), Path('sources.csv'), Path('home.csv'))

    def test_research_enriches_without_removing_existing_values(self):
        out = self.apply()
        a, b, c = out['venues']
        self.assertEqual(a['mood_tag_ids'], ['mood_cozy'])
        self.assertEqual(b['mood_tag_ids'], ['mood_calm'])
        self.assertEqual(c['mood_tag_ids'], ['invented'])
        self.assertTrue(all(v['avg_budget_krw'] == 10000 for v in out['venues']))
        self.assertEqual(a['research']['sources'], ['https://example.com/review'])
        self.assertEqual({m['name'] for m in out['menus']}, {'아메리카노 (매장 메뉴판 미확인)', '기존 메뉴', '김치전', '에그타르트'})
        self.assertEqual(next(m for m in out['menus'] if m['menu_id'] == 'm1')['price_krw'], 4500)
        self.assertTrue(all(m['price_krw'] is None for m in out['menus'] if m['menu_id'].startswith('research-')))
        self.assertEqual(out['research_audit']['research_menu_duplicate'], 1)
        self.assertEqual(out['budget_basis'], '원본 예산')
        self.assertNotIn('research', c)

    def test_wrong_address_and_held_are_never_published(self):
        self.local[0]['도로명주소'] = '서울 마포구 어울마당로 99'
        self.home[0]['보류목록'] = '[{"상태":"보류"}]'
        out = self.apply()
        self.assertEqual([m['menu_id'] for m in out['menus']], ['m1', 'm2'])
        self.assertEqual(out['venues'][0]['mood_tag_ids'], ['invented'])
        self.assertEqual(out['venues'][1]['mood_tag_ids'], ['invented'])
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

    def test_research_without_mapped_mood_does_not_erase_original(self):
        self.local[0]['매장분위기'] = '낮은 테이블과 나무 가구'
        out = self.apply()
        self.assertEqual(out['venues'][0]['mood_tag_ids'], ['invented'])
        self.assertEqual(out['venues'][0]['research']['description'], '작은 카페')

    def test_menu_comma_splits_without_splitting_prices_or_parentheses(self):
        self.assertEqual(_reviewed_menu_names('라떼, 버터풀라떼, 소금빵'), ['라떼', '버터풀라떼', '소금빵'])
        self.assertEqual(_reviewed_menu_names('라떼 (2,000원), 쿠키'), ['라떼 (2,000원)', '쿠키'])
        self.home[0]['확인된 메뉴'] = '라떼 (2,000원), 쿠키 | 라떼'
        out = self.apply()
        names = {m['name'] for m in out['menus'] if m['venue_id'] == 'b'}
        self.assertEqual(names, {'쿠키', '라떼'})

    def test_consolidated_csv_preserves_original_and_checks_identity(self):
        rows = [{'venue_id':'a','매장명':'첫 매장','도로명주소':'서울 마포구 어울마당로 1',
                 '매장분위기_조사':'아늑한 카페','매장특징_조사':'작은 카페',
                 '대표메뉴_조사':'아메리카노, 케이크','출처_URL':'https://example.com/a | https://example.com/b',
                 '조사확인일':'2026-11-11','제한사항':'방문 당시 기준'}]
        with patch('prepare_team_catalog.read_export', return_value=(rows, {})):
            out = apply_research_update(self.snapshot, Path('update.csv'))
        self.assertEqual(out['venues'][0]['research']['sources'], ['https://example.com/a','https://example.com/b'])
        self.assertEqual(out['venues'][0]['avg_budget_krw'], 10000)
        self.assertEqual([m['name'] for m in out['menus']].count('아메리카노 (매장 메뉴판 미확인)'), 1)
        self.assertEqual(next(m for m in out['menus'] if m['name'] == '케이크')['price_krw'], None)

    def test_consolidated_missing_sources_and_bad_address_are_not_applied(self):
        rows = [{'venue_id':'a','매장명':'첫 매장','도로명주소':'서울 마포구 어울마당로 1',
                 '매장분위기_조사':'아늑한','매장특징_조사':'','대표메뉴_조사':'',
                 '출처_URL':'javascript:alert(1)','조사확인일':'','제한사항':''}]
        with patch('prepare_team_catalog.read_export', return_value=(rows, {})):
            out = apply_research_update(self.snapshot, Path('update.csv'))
        self.assertNotIn('research', out['venues'][0])
        self.assertEqual(out['research_audit']['missing_visitor_source'], 1)
        rows[0]['출처_URL'] = 'https://example.com/a'
        rows[0]['도로명주소'] = '서울 마포구 어울마당로 999'
        with patch('prepare_team_catalog.read_export', return_value=(rows, {})):
            out = apply_research_update(self.snapshot, Path('update.csv'))
        self.assertNotIn('research', out['venues'][0])
        self.assertEqual(out['research_audit']['identity_conflict'], 1)

    def test_consolidated_duplicate_or_non_preview_fails(self):
        rows = [{'venue_id':'a'}, {'venue_id':'a'}]
        with patch('prepare_team_catalog.read_export', return_value=(rows, {})):
            with self.assertRaises(ValueError): apply_research_update(self.snapshot, Path('update.csv'))
            self.snapshot['preview_only'] = False
            with self.assertRaises(ValueError): apply_research_update(self.snapshot, Path('update.csv'))

    def test_cli_rejects_mixed_research_sources_before_preparing(self):
        argv = ['prepare_team_catalog.py','--source-dir','input','--output','output.json',
                '--research-update','update.csv','--research-local','local.csv']
        with patch.object(sys, 'argv', argv), patch('prepare_team_catalog.prepare') as prepare:
            with self.assertRaises(SystemExit): main()
            prepare.assert_not_called()

    def test_embedded_research_keeps_existing_menu_and_price(self):
        raw = venue('a')
        raw.update({'매장분위기_조사':'아늑한 카페', '매장특징_조사':'차분한 실내',
                    '대표메뉴_조사':'김치전골, 케이크', '좌석_편의_조사':'실내 테이블',
                    '출처_URL':'https://example.com/review', '조사확인일':'2026-11-11',
                    '제한사항':'후기 당시 기준'})
        raw_menu = menu(raw, 'm1', '김치전골')
        pair = [('test', Path('test_venues.csv'), Path('test_menus.csv'))]
        metadata = {'sha256':'abc','file':'test','rows':1}
        def read(path, required):
            return ([raw] if path.name == 'test_venues.csv' else [raw_menu], metadata)
        with patch('prepare_team_catalog.source_pairs', return_value=pair), patch('prepare_team_catalog.read_export', side_effect=read):
            out = prepare(Path('unused'))
        v = out['venues'][0]
        self.assertEqual(v['mood_tag_ids'], ['mood_cozy'])
        self.assertEqual(v['avg_budget_krw'], 15000)
        self.assertEqual(v['research']['seating'], '실내 테이블')
        self.assertEqual(len(out['menus']), 2)
        self.assertEqual(out['menus'][0]['price_krw'], 8000)
        self.assertIsNone(out['menus'][1]['price_krw'])
        self.assertEqual(out['research_audit']['research_menu_duplicate'], 1)

        update = {'venue_id':'a','매장명':raw['매장명'],'도로명주소':raw['도로명주소'],
                  '매장분위기_조사':'활기찬','매장특징_조사':'덮어쓰면 안됨','대표메뉴_조사':'케이크',
                  '출처_URL':'https://example.com/other','조사확인일':'2026-11-11','제한사항':''}
        with patch('prepare_team_catalog.read_export', return_value=([update], {})):
            apply_research_update(out, Path('update.csv'))
        self.assertEqual(v['mood_tag_ids'], ['mood_cozy'])
        self.assertEqual(v['research']['description'], '차분한 실내')
        self.assertEqual(out['research_audit']['already_enriched'], 1)
        self.assertEqual(len(out['menus']), 2)

    def test_embedded_without_source_does_not_replace_values(self):
        raw = venue('a')
        raw.update({'매장분위기_조사':'아늑한', '매장특징_조사':'추정', '출처_URL':''})
        pair = [('test', Path('test_venues.csv'), Path('test_menus.csv'))]
        def read(path, required):
            return ([raw] if path.name == 'test_venues.csv' else [], {'sha256':'abc','file':'test','rows':1})
        with patch('prepare_team_catalog.source_pairs', return_value=pair), patch('prepare_team_catalog.read_export', side_effect=read):
            out = prepare(Path('unused'))
        self.assertNotIn('research', out['venues'][0])
        self.assertNotIn('mood_cozy', out['venues'][0]['mood_tag_ids'])


if __name__ == '__main__':
    unittest.main()
