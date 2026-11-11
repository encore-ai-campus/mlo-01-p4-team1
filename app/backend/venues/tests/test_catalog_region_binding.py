import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.venues.repositories import csv_lazy_repo


class CatalogRegionBindingTests(unittest.TestCase):
    @staticmethod
    def write_venue(path, identifier, name, region):
        fields = ['venue_id', '매장명', '자치구', '도로명주소', '업종구분']
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow({'venue_id': identifier, '매장명': name, '자치구': region,
                             '도로명주소': f'{name} 주소', '업종구분': '카페'})

    def test_incheon_id_never_creates_a_seoul_alias_and_seoul_alias_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seoul = root / 'seoul' / 'by_gu'
            incheon = root / 'incheon' / 'by_gu'
            seoul.mkdir(parents=True)
            incheon.mkdir(parents=True)
            fields = ['venue_id', '매장명', '자치구', '도로명주소', '업종구분']
            for folder, region, identifier, name in (
                (incheon, '중구', 'VN_IC_000001', '인천 매장'),
                (seoul, '중구', 'VN_SL_000001', '서울 매장'),
            ):
                with (folder / f'{region}_venues.csv').open('w', encoding='utf-8-sig', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow({'venue_id': identifier, '매장명': name, '자치구': region,
                                     '도로명주소': f'{name} 주소', '업종구분': '카페'})

            with patch.object(csv_lazy_repo, 'REGION_CSV_DIRS', [incheon, seoul]), \
                 patch.object(csv_lazy_repo, 'BY_GU_DIR', root / 'none'), \
                 patch.object(csv_lazy_repo, 'FALLBACK_VENUES_CANDIDATES', []), \
                 patch.object(csv_lazy_repo, 'FALLBACK_MENUS_CANDIDATES', []):
                repo = csv_lazy_repo.CsvLazyRepository()
                self.assertEqual(repo.get_venue_by_id('VN_IC_000001')['venue_name'], '인천 매장')
                self.assertNotIn('venue_seoul_0001', repo._venue_map)
                self.assertEqual(repo.get_venue_by_id('venue_seoul_0001')['venue_name'], '서울 매장')
                self.assertEqual(repo.get_venue_by_id('VN_SL_000001')['venue_name'], '서울 매장')

    def test_real_region_csv_wins_over_preloaded_sample_and_can_be_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seoul = root / 'seoul' / 'by_gu'
            seoul.mkdir(parents=True)
            actual = seoul / '중구_venues.csv'
            sample = root / 'sample.csv'
            self.write_venue(actual, 'VN_SL_000777', '변경 전 실제 매장', '중구')
            self.write_venue(sample, 'VN_SL_000001', '시연 매장', '중구')
            with patch.object(csv_lazy_repo, 'REGION_CSV_DIRS', [seoul]), \
                 patch.object(csv_lazy_repo, 'BY_GU_DIR', root / 'none'), \
                 patch.object(csv_lazy_repo, 'FALLBACK_VENUES_CANDIDATES', [sample]), \
                 patch.object(csv_lazy_repo, 'FALLBACK_MENUS_CANDIDATES', []):
                repo = csv_lazy_repo.CsvLazyRepository()
                self.assertEqual([v['venue_name'] for v in repo.get_venues_by_gu('중구')], ['변경 전 실제 매장'])
                self.assertEqual([v['venue_name'] for v in repo.get_source_venues_by_region('중구')], ['변경 전 실제 매장'])
                self.assertEqual(repo.get_source_venues_by_region('강남구'), [])
                self.assertEqual(repo.get_venue_by_id('VN_SL_000777')['venue_name'], '변경 전 실제 매장')
                self.assertIsNone(repo.get_source_venue_by_id('VN_SL_000001'))
                self.write_venue(actual, 'VN_SL_000777', '변경 후 실제 매장', '중구')
                self.assertEqual(repo.get_source_venue_by_id('VN_SL_000777')['venue_name'], '변경 후 실제 매장')
                repo.refresh_source_data()
                self.assertEqual(repo.get_venue_by_id('VN_SL_000777')['venue_name'], '변경 후 실제 매장')

    def test_approval_source_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seoul = root / 'seoul' / 'by_gu'
            seoul.mkdir(parents=True)
            self.write_venue(seoul / '중구_venues.csv', 'VN_SL_000001', '매장 A', '중구')
            self.write_venue(seoul / '강남구_venues.csv', 'VN_SL_000001', '매장 B', '강남구')
            with patch.object(csv_lazy_repo, 'REGION_CSV_DIRS', [seoul]), \
                 patch.object(csv_lazy_repo, 'BY_GU_DIR', root / 'none'), \
                 patch.object(csv_lazy_repo, 'FALLBACK_VENUES_CANDIDATES', []), \
                 patch.object(csv_lazy_repo, 'FALLBACK_MENUS_CANDIDATES', []):
                repo = csv_lazy_repo.CsvLazyRepository()
                with self.assertRaisesRegex(ValueError, 'Duplicate venue ID'):
                    repo.get_source_venue_by_id('VN_SL_000001')


if __name__ == '__main__':
    unittest.main()
