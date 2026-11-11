"""Operational catalog opt-in is explicit; this does not verify publication rights."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from venue_public_catalog import read_public_catalog


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


if __name__ == '__main__':
    unittest.main()
