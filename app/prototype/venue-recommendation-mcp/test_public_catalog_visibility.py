"""Operational catalog opt-in is explicit; this does not verify publication rights."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from venue_public_catalog import PublicCatalogReader, read_public_catalog


class PublicCatalogVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='public-catalog-visibility-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'catalog.json'

    def write(self, **extra):
        self.path.write_text(json.dumps({'version': 'test', 'venues': [], 'menus': [], **extra}), encoding='utf-8')

    def read(self, *, local=False):
        return read_public_catalog(self.path, {}, {}, allow_preview=local)

    def test_operational_mode_rejects_missing_and_true_flag(self):
        self.write()
        with self.assertRaisesRegex(ValueError, 'preview_only=false'):
            self.read()
        self.write(preview_only=True)
        with self.assertRaisesRegex(ValueError, '시연용'):
            self.read()

    def test_operational_mode_requires_boolean_false(self):
        self.write(preview_only=False)
        self.assertEqual(self.read()[0]['metadata']['preview_only'], False)
        self.write(preview_only='false')
        with self.assertRaisesRegex(ValueError, '스냅샷을 읽을 수 없습니다'):
            self.read()

    def test_local_mode_accepts_existing_legacy_and_preview_catalogs(self):
        self.write()
        self.assertEqual(self.read(local=True)[0]['version'], 'test')
        self.write(preview_only=True)
        self.assertTrue(self.read(local=True)[0]['metadata']['preview_only'])

    def test_reader_reuses_an_unchanged_snapshot(self):
        self.write(preview_only=False)
        reader = PublicCatalogReader(self.path, {}, {})
        self.assertIs(reader.read(), reader.read())

    def test_atomic_replacement_removes_a_venue_from_the_cached_catalog(self):
        region = {'sido': '서울특별시', 'sigungu': '마포구', 'name': '서교동', 'nx': 59, 'ny': 126}
        venue = {'venue_id': 'demo-1', 'name': '테스트 카페', 'address': '서울 마포구 테스트 주소',
                 'region_id': 'demo-region', 'venue_category': 'CAFE', 'is_public': True}
        self.write(preview_only=False, venues=[venue])
        reader = PublicCatalogReader(self.path, {'demo-region': region}, {})
        self.assertEqual(len(reader.read()[0]['venues']), 1)

        replacement = self.path.with_name('replacement.json')
        replacement.write_text(json.dumps({
            'version': 'test', 'preview_only': False, 'venues': [{**venue, 'is_public': False}], 'menus': [],
        }), encoding='utf-8')
        replacement.replace(self.path)
        catalog, locations = reader.read()
        self.assertEqual(catalog['venues'], [])
        self.assertEqual(locations, {})

    def test_reader_rejects_invalid_replacement_instead_of_serving_cached_data(self):
        self.write(preview_only=False)
        reader = PublicCatalogReader(self.path, {}, {})
        reader.read()

        replacement = self.path.with_name('replacement.json')
        replacement.write_text(json.dumps({
            'version': 'test', 'preview_only': True, 'venues': [], 'menus': [],
        }), encoding='utf-8')
        replacement.replace(self.path)
        with self.assertRaisesRegex(ValueError, '시연용'):
            reader.read()


if __name__ == '__main__':
    unittest.main()
