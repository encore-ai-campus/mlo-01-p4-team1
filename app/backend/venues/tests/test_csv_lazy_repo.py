import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase
from backend.common.data_paths import discover_analysis_dirs
from backend.venues.repositories.csv_lazy_repo import get_venue_repo, CsvLazyRepository


class TestCsvLazyRepository(TestCase):
    def setUp(self):
        self.repo = get_venue_repo()
        self.repo.clear_cache()

    def test_gangnam_venues_lazy_loading_and_speed(self):
        """강남구 매장 목록 조회 및 0.01초 이내 초고속 지연 로딩 검증"""
        # 1회차: 디스크 지연 로딩 + 인덱싱
        t0 = time.perf_counter()
        venues = self.repo.get_venues_by_gu('강남구')
        t1 = time.perf_counter()
        elapsed_first = t1 - t0

        self.assertGreaterEqual(len(venues), 10, "강남구 매장 목록이 10개 이상이어야 합니다.")
        # 첫 번째 로드(매장 3,212개 + 메뉴 51,392개 PK 조인 및 예산 산출)도 0.50초 이내에 완료되어야 함
        self.assertLess(elapsed_first, 0.50, f"강남구 1차 로딩 시간({elapsed_first:.4f}s) 초과")

        # 2회차: 인메모리 캐시 룩업 (0.01초 이내 필수 기준)
        t2 = time.perf_counter()
        venues_cached = self.repo.get_venues_by_gu('강남구')
        t3 = time.perf_counter()
        elapsed_cached = t3 - t2

        self.assertEqual(len(venues), len(venues_cached))
        self.assertLess(elapsed_cached, 0.01, f"캐시 조회 시간({elapsed_cached:.6f}s)이 0.01초 기준을 초과했습니다.")
        print(f"\n[검증 결과 1] 강남구 매장 {len(venues)}개 조회: 1차 로딩={elapsed_first*1000:.2f}ms, 캐시 조회={elapsed_cached*1000:.3f}ms (< 10ms 통과)")

    def test_venue_menus_loading_and_16_items(self):
        """특정 venue_id의 16개 메뉴판 조회 및 0.01초 이내 반환 검증"""
        gangnam_venues = self.repo.get_venues_by_gu('강남구')
        self.assertTrue(len(gangnam_venues) > 0)
        target_venue_id = gangnam_venues[0]['venue_id']

        # 메뉴 조회 시간 측정
        t0 = time.perf_counter()
        menus = self.repo.get_menus_by_venue(target_venue_id)
        t1 = time.perf_counter()
        elapsed = t1 - t0

        self.assertEqual(len(menus), 16, f"매장({target_venue_id})의 메뉴 개수는 정확히 16개여야 합니다. (실제: {len(menus)}개)")
        self.assertLess(elapsed, 0.01, f"메뉴 조회 시간({elapsed:.6f}s)이 0.01초 기준을 초과했습니다.")
        print(f"[검증 결과 2] 매장({target_venue_id}) 16개 메뉴판 조회 소요 시간: {elapsed*1000:.3f}ms (< 10ms 통과, 메뉴수={len(menus)})")

    def test_single_venue_lookup(self):
        """단일 매장 O(1) 해시 룩업 검증"""
        gangnam_venues = self.repo.get_venues_by_gu('강남구')
        target = gangnam_venues[0]
        venue = self.repo.get_venue_by_id(target['venue_id'])
        self.assertIsNotNone(venue)
        self.assertEqual(venue['venue_name'], target['venue_name'])

    def test_env_data_dir_priority(self):
        """VENUE_DATA_DIR 환경변수가 지정되면 최우선 탐색 경로로 포함되어야 함"""
        with tempfile.TemporaryDirectory() as tmp:
            custom_dir = Path(tmp) / 'custom_analysis'
            custom_dir.mkdir()
            with patch.dict(os.environ, {'VENUE_DATA_DIR': str(custom_dir)}, clear=False):
                candidates = discover_analysis_dirs()
            self.assertGreaterEqual(len(candidates), 1)
            self.assertEqual(candidates[0].resolve(), custom_dir.resolve())
