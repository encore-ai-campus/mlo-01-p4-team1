"""운영 사용자 권한과 팀 카탈로그의 목록·상세 연결 검사."""

from contextlib import closing
import gzip
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from backend.accounts.models import MemberProfile
from backend.venues.models.catalog import VenueCatalogRecord
from backend.venues.models.status_overlay import VenueStatusOverlay
from backend.venues.views.concierge_view import _catalog_detail_capability
from backend.workspaces.models import ServiceMembership


@override_settings(LOCAL_DEMO=False, MEMBER_LOCAL_PREVIEW=False, PLATFORM_ENABLED=False, ALLOWED_HOSTS=['testserver'])
class OperationalCatalogBridgeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for kind in ('CONSUMER', 'BUSINESS'):
            user = get_user_model().objects.create_user(username=f'catalog_{kind.lower()}')
            MemberProfile.objects.create(user=user, nickname=kind, nickname_key=kind.lower())
            ServiceMembership.objects.create(user=user, kind=kind, status='ACTIVE')
            setattr(cls, kind.lower(), user)

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'catalog.sqlite3'
        self.rows = [
            {'venue_id': 'VN_SL_TEST1', '매장명': '테스트 카페', '도로명주소': '서울특별시 강남구 테스트로 1',
             '업종구분': '카페', '세부카테고리': '카페', '영업상태_코드': 'OPEN',
             '자치구': '강남구', '행정동': '역삼동', '1인_평균예산(원)': '8000',
             '위도_lat': '37.5', '경도_lng': '127.04'},
            {'venue_id': 'VN_IC_TEST2', '매장명': '인천 테스트 카페', '도로명주소': '인천광역시 연수구 테스트로 2',
             '업종구분': '카페', '세부카테고리': '카페', '영업상태_코드': '1',
             '자치구': '연수구', '행정동': '송도동', '1인_평균예산(원)': '9000',
             '위도_lat': '37.4', '경도_lng': '126.66'},
            {'venue_id': 'VN_SL_DAYCARE', '매장명': '테스트어린이집', '도로명주소': '서울특별시 강남구 테스트로 3',
             '업종구분': '술집', '세부카테고리': '생맥주 전문', '영업상태_코드': 'OPEN',
             '자치구': '강남구', '행정동': '역삼동'},
        ]
        self.menu = {'menu_id': 'MN_TEST1', 'venue_id': 'VN_SL_TEST1', '매장명': '테스트 카페',
                     '메뉴명': '테스트 라떼', '가격(원)': '5000', '대표메뉴여부': 'Y'}
        raw_rows = [json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in self.rows]
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE venues (venue_id TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE TABLE menus (menu_id TEXT NOT NULL, venue_id TEXT NOT NULL, raw TEXT NOT NULL);
                CREATE INDEX venues_id ON venues (venue_id);
                CREATE INDEX menus_venue_id ON menus (venue_id);
            ''')
            metadata = {'schema': 1, 'version': 'integration-test', 'preview_only': False}
            db.execute('INSERT INTO metadata VALUES (?, ?)', ('catalog', json.dumps(metadata)))
            db.execute('INSERT INTO metadata VALUES (?, ?)',
                       ('venue_rows_gzip', sqlite3.Binary(gzip.compress(('\n'.join(raw_rows) + '\n').encode()))))
            db.executemany('INSERT INTO venues VALUES (?, ?)',
                           [(row['venue_id'], raw) for row, raw in zip(self.rows, raw_rows)])
            db.execute('INSERT INTO menus VALUES (?, ?, ?)',
                       (self.menu['menu_id'], self.menu['venue_id'], json.dumps(self.menu, ensure_ascii=False)))
            db.commit()
        self.environment = patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        VenueCatalogRecord.objects.create(venue_id='owner_private', dataset_version='owner_direct',
                                          venue_name='미승인 매장', gu_name='강남구')

    def test_list_detail_and_menus_use_the_same_published_venue(self):
        self.client.force_login(self.consumer)
        listing = self.client.get('/api/venues', {'sigungu': '강남구'})
        self.assertEqual(listing.status_code, 200, listing.content)
        rows = listing.json()['venues']
        self.assertEqual([row['venue_id'] for row in rows], ['VN_SL_TEST1'])
        self.assertEqual(rows[0]['avg_price'], 8000)
        self.assertIsNone(rows[0]['is_open_now'])
        self.assertEqual(self.client.get('/api/venues', {'sigungu': '강남구', 'open_now': '1'}).json()['venues'], [])
        detail = self.client.get('/api/venues/VN_SL_TEST1')
        self.assertEqual(detail.status_code, 200, detail.content)
        venue = detail.json()['venue']
        self.assertEqual((venue['venue_id'], venue['venue_name']), ('VN_SL_TEST1', '테스트 카페'))
        self.assertIsNone(venue['is_open_now'])
        menus = [menu for tab in venue['menu_tabs'] for menu in tab['items']]
        self.assertEqual({item['menu_id'] for item in menus}, {'MN_TEST1'})
        self.assertEqual(self.client.get('/api/venues/owner_private').status_code, 404)
        self.assertEqual(self.client.get('/api/venues/VN_SL_DAYCARE').status_code, 404)

    def test_authentication_region_isolation_and_hidden_venue(self):
        self.assertEqual(Client().get('/api/venues').status_code, 401)
        business = Client()
        business.force_login(self.business)
        self.assertEqual(business.get('/api/venues').status_code, 403)
        self.client.force_login(self.consumer)
        response = self.client.get('/api/venues', {'sigungu': '연수구'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row['venue_id'] for row in response.json()['venues']], ['VN_IC_TEST2'])
        VenueStatusOverlay.objects.create(venue_id='VN_SL_TEST1', status='CLOSED_HIDDEN')
        self.assertEqual(self.client.get('/api/venues', {'sigungu': '강남구'}).json()['venues'], [])
        self.assertEqual(self.client.get('/api/venues/VN_SL_TEST1').status_code, 404)

    def test_missing_or_invalid_catalog_never_falls_back_to_demo_data(self):
        self.client.force_login(self.consumer)
        with patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': str(self.path.parent / 'missing.sqlite3')}):
            response = self.client.get('/api/venues')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()['code'], 'VENUE_CATALOG_UNAVAILABLE')

    def test_only_the_sqlite_catalog_advertises_operational_detail(self):
        result = {'catalog_source': 'PUBLIC_SNAPSHOT', 'catalog_metadata': {'preview_only': False}}
        self.assertTrue(_catalog_detail_capability(result)['catalog_metadata']['detail_supported'])
        with patch.dict(os.environ, {'VENUE_MCP_PUBLIC_CATALOG': str(self.path.with_suffix('.json'))}):
            self.assertNotIn('detail_supported', _catalog_detail_capability(result)['catalog_metadata'])
        with override_settings(MEMBER_LOCAL_PREVIEW=True):
            self.assertTrue(_catalog_detail_capability(result)['catalog_metadata']['detail_supported'])
