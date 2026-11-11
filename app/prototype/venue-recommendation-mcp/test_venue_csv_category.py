"""Explicit cafe categories remain searchable with specific subtypes."""

import unittest

from venue_csv_catalog import csv_category


class CsvCategoryTests(unittest.TestCase):
    def test_cafe_subtypes_and_missing_subtype_remain_cafes(self):
        for subtype in ('카페', '와플/벨기에디저트카페', '베이커리', ''):
            with self.subTest(subtype=subtype):
                self.assertEqual(csv_category({'업종구분': '카페', '세부카테고리': subtype}), 'CAFE')

    def test_unconfirmed_pubs_and_other_industries_remain_unclassified(self):
        self.assertIsNone(csv_category({'업종구분': '술집', '세부카테고리': '요리 주점'}))
        self.assertIsNone(csv_category({'업종구분': '음식점', '세부카테고리': '카페'}))
        self.assertEqual(csv_category({'업종구분': '술집', '세부카테고리': '이자카야'}), 'PUB')


if __name__ == '__main__':
    unittest.main()
